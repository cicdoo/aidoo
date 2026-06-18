# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Generic, provider-agnostic CLI runner.

Owns the orchestration that is identical for every AI CLI — the background
worker thread, the stdout pump + deadline loop, persisting the streamed message
to the DB, and broadcasting over the Odoo bus. Everything provider-specific is
delegated to the session's adapter (``aidoo.provider.<code>``):

* ``adapter._prepare_run(ctx)``     -> argv / env / cwd / stdin / cleanup
* ``adapter._normalize_event(evt)`` -> a stream of common ops this runner applies
* ``adapter._post_run(...)``        -> optional fallback text
"""
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time

from odoo import api, models
from odoo.modules.registry import Registry

_logger = logging.getLogger(__name__)


class AiAssistantRunner(models.AbstractModel):
    _name = "aidoo.runner"
    _description = "Aidoo AI Assistant CLI Runner"

    def _adapter(self, session):
        """The provider adapter for this session."""
        return self.env["aidoo.provider.%s" % session.provider]

    # ------------------------------------------------------------------
    # Launch (runs in the request env, as the user)
    # ------------------------------------------------------------------
    def _launch(self, session, prompt, is_first, token):
        adapter = self._adapter(session)
        cli_path = adapter._resolve_cli_path()
        scratch = session._get_scratch_dir()
        model = adapter._model()
        max_turns = int(session._config("max_turns", 30))
        timeout_s = int(session._config("timeout_s", 900))
        base_url = session._config("base_url", "http://127.0.0.1:8069")
        python_bin = session._config("python_bin", "") or sys.executable
        target_db = session._target_db()
        routing_sid = session._mint_routing_sid(target_db)
        bridge_script = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "bridge", "mcp_server.py")
        oauth_env = adapter._oauth_env(session.user_id)
        # Align the CLI's stored account identity with this Odoo user (Claude).
        adapter._sync_cli_account(session.user_id)
        allowed_tools = sorted(session._effective_tools())
        excluded_models = sorted(session._ai_excluded_models())
        identity = session.user_id._ai_identity_prompt(set(allowed_tools))

        # ``is_first`` is turn-based (passed by send_message). Each adapter decides
        # internally how to honor resume vs. start fresh (Claude checks its
        # transcript on disk; Codex resumes by thread id; Gemini/Grok replay
        # history into the prompt).
        ctx = {
            "provider": session.provider,
            "dbname": self.env.cr.dbname,
            "uid": session.user_id.id,
            "session_id": session.id,
            "cli_session_id": session.cli_session_id,
            "prompt": prompt,
            "is_first": is_first,
            "cli_path": cli_path,
            "scratch": scratch,
            "uploads_dir": session._uploads_dir(create=True),
            "model": model,
            "max_turns": max_turns,
            "timeout_s": timeout_s,
            "base_url": base_url,
            "python_bin": python_bin,
            "target_db": target_db,
            "routing_sid": routing_sid,
            "bridge_script": bridge_script,
            "token": token,
            "oauth_env": oauth_env,
            "allowed_tools": allowed_tools,
            "excluded_models": excluded_models,
            "identity": identity,
            "output_format": adapter._pconfig("output_format", ""),
        }
        thread = threading.Thread(
            target=self._run_worker, args=(ctx,), daemon=True,
            name="aidoo_run_%s" % session.id)
        self.env.cr.postcommit.add(thread.start)

    # ------------------------------------------------------------------
    # Background worker (own cursor)
    # ------------------------------------------------------------------
    def _run_worker(self, ctx):
        registry = Registry(ctx["dbname"])
        with registry.cursor() as cr:
            env = api.Environment(cr, ctx["uid"], {})
            session = env["aidoo.session"].browse(ctx["session_id"])
            runner = env["aidoo.runner"]
            try:
                runner._execute(env, session, ctx)
            except Exception as e:
                _logger.exception("AI assistant run failed")
                runner._finalize_pending_tools(env, session, "error")
                session.write({"state": "error", "last_error": str(e)})
                runner._emit(session, {"kind": "error", "error": str(e)})
                cr.commit()

    def _execute(self, env, session, ctx):
        adapter = self._adapter(session)
        prep = adapter._prepare_run(ctx)
        argv, run_env = prep["argv"], prep["env"]
        cwd, stdin_data, cleanup = prep["cwd"], prep.get("stdin"), prep.get("cleanup") or []

        _logger.info("AI assistant: spawning %s provider=%s (session %s)",
                     ctx["cli_path"], ctx["provider"], ctx["session_id"])
        proc = subprocess.Popen(
            argv, cwd=cwd, env=run_env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=(subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL),
            text=True, bufsize=1)
        if stdin_data is not None:
            try:
                proc.stdin.write(stdin_data)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        session.write({"last_run_pid": proc.pid})
        self._emit(session, {"kind": "started"})
        env.cr.commit()

        deadline = time.time() + ctx["timeout_s"]
        msg_map = {}          # cli message id -> aidoo.message record id
        state = {}            # adapter scratchpad for stream parsing
        produced_text = {"any": False}
        line_q = queue.Queue()

        def _pump(pipe, q):
            try:
                for ln in pipe:
                    q.put(ln)
            finally:
                q.put(None)

        reader = threading.Thread(
            target=_pump, args=(proc.stdout, line_q), daemon=True,
            name="aidoo_read_%s" % ctx["session_id"])
        reader.start()
        try:
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    proc.kill()
                    raise TimeoutError("Run exceeded timeout")
                try:
                    line = line_q.get(timeout=min(remaining, 5))
                except queue.Empty:
                    if proc.poll() is not None:
                        break
                    continue
                if line is None:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except ValueError:
                    continue
                for op in adapter._normalize_event(evt, state):
                    self._apply_op(env, session, op, msg_map, produced_text)
                env.cr.commit()
            try:
                rc = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = proc.wait()
        finally:
            for p in cleanup:
                try:
                    os.remove(p)
                except OSError:
                    pass
            env["aidoo.session"]._delete_routing_sid(ctx.get("routing_sid"))

        stderr = proc.stderr.read() if proc.stderr else ""

        # Fallback: some CLIs don't stream a final assistant message; recover it.
        if not produced_text["any"] and session.state == "running":
            fallback = adapter._post_run(ctx, rc, stderr)
            if fallback:
                rec = env["aidoo.message"].create({
                    "session_id": session.id, "role": "assistant", "body": fallback})
                self._emit_message(session, rec)
                produced_text["any"] = True

        session.write({"last_run_pid": False, "turn_count": session.turn_count + 1})
        # Any tool call still marked "running" never received its result event
        # (CLI exited mid-tool, timed out, or was stopped). Settle it so the
        # chip doesn't spin forever once the frontend reloads.
        self._finalize_pending_tools(
            env, session, "done" if rc == 0 else "error", msg_map)
        if session.state == "running":
            if rc != 0:
                session.write({"state": "error", "last_error": stderr[:2000]})
                self._emit(session, {"kind": "error",
                                     "error": stderr[:500] or "CLI exited %s" % rc})
            else:
                session.write({"state": "done"})
                self._emit(session, {"kind": "done"})
        env.cr.commit()

    # ------------------------------------------------------------------
    # Apply one normalized op to the DB + bus
    # ------------------------------------------------------------------
    def _apply_op(self, env, session, op, msg_map, produced_text):
        kind = op.get("op")
        if kind == "session_id":
            if op.get("id") and session.cli_session_id != op["id"]:
                session.cli_session_id = op["id"]
        elif kind == "text":
            rec = self._get_msg(env, session, op.get("msg_id"), msg_map)
            text = op.get("text") or ""
            if text:
                rec.body = text if op.get("replace") else (rec.body or "") + text
                produced_text["any"] = True
                self._emit_message(session, rec)
        elif kind == "tool_use":
            rec = self._get_msg(env, session, op.get("msg_id"), msg_map)
            calls = list(rec.tool_calls or [])
            if not any(c.get("id") == op.get("id") for c in calls):
                calls.append({
                    "id": op.get("id"), "name": op.get("name"),
                    "input": op.get("input") or {}, "result": None,
                    "status": "running"})
                rec.tool_calls = calls
                self._emit_message(session, rec)
        elif kind == "tool_result":
            self._patch_tool_result(env, session, op, msg_map)
        elif kind == "done":
            # A final result event often repeats the full assistant text. Only
            # materialize it when nothing streamed this turn — otherwise it would
            # duplicate the message already built from the streamed text blocks.
            if op.get("text") and not produced_text["any"]:
                rec = self._get_msg(env, session, op.get("msg_id"), msg_map)
                rec.body = op["text"]
                produced_text["any"] = True
                self._emit_message(session, rec)
            session.write({"state": "done"})
            self._emit(session, {"kind": "done"})
        elif kind == "error":
            txt = op.get("text") or ""
            session.write({"state": "error", "last_error": txt[:2000]})
            self._emit(session, {"kind": "error", "error": txt[:500]})

    def _get_msg(self, env, session, cid, msg_map):
        """Get (or create) the assistant message record for a CLI message id.

        Providers that don't tag messages with ids share one per-turn record
        under the ``__default__`` key."""
        key = cid or "__default__"
        rec_id = msg_map.get(key)
        Msg = env["aidoo.message"]
        if rec_id:
            return Msg.browse(rec_id)
        rec = Msg.create({
            "session_id": session.id, "role": "assistant",
            "cli_msg_id": cid or False, "body": "", "tool_calls": False})
        msg_map[key] = rec.id
        return rec

    def _patch_tool_result(self, env, session, op, msg_map):
        tid = op.get("id")
        if not tid:
            return
        Msg = env["aidoo.message"]
        for rec_id in msg_map.values():
            rec = Msg.browse(rec_id)
            # Rebuild with fresh dicts so the assignment differs from the cached
            # value; mutating the cached list in place would make Odoo treat it
            # as unchanged and skip the DB write, leaving the chip stuck on
            # "running" once the frontend reloads from the DB.
            calls = [dict(c) for c in (rec.tool_calls or [])]
            changed = False
            for call in calls:
                if call.get("id") == tid:
                    call["result"] = (op.get("text") or "")[:4000]
                    call["status"] = "error" if op.get("is_error") else "done"
                    changed = True
            if changed:
                rec.tool_calls = calls
                self._emit_message(session, rec)

    def _finalize_pending_tools(self, env, session, status, msg_map=None):
        """Settle any tool calls left in the ``running`` state at turn end.

        A call stays ``running`` when its result event never streamed (CLI
        exited mid-tool, timed out, or was stopped). Flip those to a terminal
        status so the chip stops spinning. Scans this turn's records when
        ``msg_map`` is given, else every message in the session (error path)."""
        Msg = env["aidoo.message"]
        if msg_map is not None:
            recs = Msg.browse([r for r in msg_map.values()])
        else:
            recs = session.message_ids
        for rec in recs:
            # Rebuild with fresh dicts; mutating the cached list in place looks
            # unchanged to Odoo and the write would be skipped.
            calls = [dict(c) for c in (rec.tool_calls or [])]
            changed = False
            for call in calls:
                if call.get("status") not in ("done", "error"):
                    call["status"] = status
                    changed = True
            if changed:
                rec.tool_calls = calls
                self._emit_message(session, rec)

    # ------------------------------------------------------------------
    # Bus
    # ------------------------------------------------------------------
    def _emit(self, session, payload):
        payload = dict(payload, session_id=session.id)
        session.user_id.partner_id._bus_send("aidoo", payload)

    def _emit_message(self, session, rec):
        self._emit(session, {"kind": "message", "message": rec._to_frontend()[0]})
