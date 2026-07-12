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

try:
    import resource
except ImportError:  # non-Unix (dev only; production is Linux)
    resource = None

from odoo import api, models
from odoo.modules.registry import Registry

_logger = logging.getLogger(__name__)


def _lift_rlimit_as():
    """Raise the child's RLIMIT_AS soft limit back to the hard limit.

    Odoo workers cap their address space at ``limit_memory_hard`` (soft only;
    the hard limit stays unlimited). Bun-based CLIs (Claude) reserve large JIT
    address ranges far beyond their RSS and die mid-run with a SIGILL panic
    under the inherited cap. Runs as ``preexec_fn`` between fork and exec, so
    it must stay minimal: no imports, no logging.
    """
    try:
        _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        resource.setrlimit(resource.RLIMIT_AS, (hard, hard))
    except (ValueError, OSError):
        pass


class AiAssistantRunner(models.AbstractModel):
    _name = "aidoo.runner"
    _description = "Aidoo AI Assistant CLI Runner"

    def _adapter(self, session):
        """The provider adapter for this session."""
        return self.env["aidoo.provider.%s" % session.provider]

    # ------------------------------------------------------------------
    # Launch (runs in the request env, as the user)
    # ------------------------------------------------------------------
    def _launch(self, session, prompt, is_first, token, pending_audio=None):
        adapter = self._adapter(session)
        # Native (in-process) providers spawn no subprocess: skip CLI path
        # resolution, the loopback bridge token/routing sid, and the CLI auth env.
        native = adapter._is_native
        cli_path = "" if native else adapter._resolve_cli_path()
        scratch = session._get_scratch_dir()
        model = adapter._model()
        max_turns = int(session._config("max_turns", 30))
        timeout_s = int(session._config("timeout_s", 900))
        base_url = session._config("base_url", "http://127.0.0.1:8069")
        python_bin = session._config("python_bin", "") or sys.executable
        target_db = session._target_db()
        routing_sid = "" if native else session._mint_routing_sid(target_db)
        bridge_script = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "bridge", "mcp_server.py")
        # Credentials load under the credential user (the persona's dedicated
        # service account when persona-driven, else the invoking user). Tools
        # still run as session.user_id — see ctx["uid"] below.
        cred_user = session._credential_user()
        oauth_env = {} if native else adapter._oauth_env(cred_user)
        # Align the CLI's stored account identity with the credential user (Claude).
        if not native:
            adapter._sync_cli_account(cred_user)
        allowed_tools = sorted(session._effective_tools())
        excluded_models = sorted(session._ai_excluded_models())
        identity = session._run_identity(set(allowed_tools))

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
            # Audio uploads transcribed in the worker thread, before dispatch,
            # so the transcript files exist when the CLI Reads them.
            "pending_audio": pending_audio or [],
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
                if ctx.get("pending_audio"):
                    session._transcribe_pending(ctx["pending_audio"])
                    cr.commit()
                if runner._adapter(session)._is_native:
                    runner._execute_native(env, session, ctx)
                else:
                    runner._execute(env, session, ctx)
            except Exception as e:
                _logger.exception("AI assistant run failed")
                runner._finalize_pending_tools(env, session, "error")
                session.write({"state": "error", "last_error": str(e)})
                runner._emit(session, {"kind": "error", "error": str(e)})
                cr.commit()
                try:
                    session._deliver_result(False)
                except Exception:  # noqa: BLE001
                    _logger.exception("AI assistant: result delivery failed")

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
            text=True, bufsize=1,
            preexec_fn=(_lift_rlimit_as if resource else None))
        if stdin_data is not None:
            try:
                proc.stdin.write(stdin_data)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                _logger.debug(
                    "AI assistant: writing stdin to %s failed (process already "
                    "gone?)", ctx["provider"], exc_info=True)

        session.write({"last_run_pid": proc.pid})
        self._emit(session, {"kind": "started"})
        env.cr.commit()

        deadline = time.time() + ctx["timeout_s"]
        # Keep the Discuss "typing…" indicator alive across long runs (clients
        # clear it after 60s without a refresh).
        next_typing = time.time() + 40
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

        # Drain stderr concurrently into a buffer. Reading it only after the
        # process exits can deadlock: a CLI that writes more than the pipe buffer
        # (~64KB) to stderr blocks on that write, stops producing stdout, and the
        # run hangs. A dedicated pump keeps the pipe empty so the CLI never blocks.
        stderr_buf = []

        def _drain_stderr(pipe, buf):
            try:
                for ln in pipe:
                    buf.append(ln)
            except (ValueError, OSError):
                pass

        reader = threading.Thread(
            target=_pump, args=(proc.stdout, line_q), daemon=True,
            name="aidoo_read_%s" % ctx["session_id"])
        reader.start()
        stderr_reader = None
        if proc.stderr:
            stderr_reader = threading.Thread(
                target=_drain_stderr, args=(proc.stderr, stderr_buf), daemon=True,
                name="aidoo_err_%s" % ctx["session_id"])
            stderr_reader.start()
        try:
            while True:
                if session.discuss_channel_id and time.time() > next_typing:
                    session._discuss_set_typing(True)
                    next_typing = time.time() + 40
                    env.cr.commit()
                remaining = deadline - time.time()
                if remaining <= 0:
                    proc.kill()
                    raise TimeoutError("Run exceeded timeout")
                try:
                    line = line_q.get(timeout=max(0.05, min(remaining, 5)))
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
                    _logger.debug("AI assistant: dropping non-JSON line: %.200s",
                                  line)
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

        if stderr_reader is not None:
            stderr_reader.join(timeout=5)
        stderr = "".join(stderr_buf)
        # A CLI can exit 0 yet still print warnings to stderr — e.g. Codex noting
        # it dropped an MCP server that lost the startup race. That stderr is
        # discarded on the success path below, so surface it at debug level for
        # diagnosing "tools not available" turns without changing behavior.
        if stderr.strip():
            _logger.debug("AI assistant: %s (session %s) stderr:\n%s",
                          ctx["provider"], ctx["session_id"], stderr[:4000])

        # Fallback: some CLIs don't stream a final assistant message; recover it.
        fallback_rec = env["aidoo.message"]
        if not produced_text["any"] and session.state == "running":
            fallback = adapter._post_run(ctx, rc, stderr)
            if fallback:
                fallback_rec = env["aidoo.message"].create({
                    "session_id": session.id, "role": "assistant", "body": fallback})
                self._emit_message(session, fallback_rec)
                produced_text["any"] = True

        # The fallback record is not in msg_map — include it explicitly.
        self._persist_turn_artifacts(
            env, session,
            env["aidoo.message"].browse(list(msg_map.values())) | fallback_rec)
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
        # Notify non-chat origins (scheduled agents, Discuss bot). A delivery
        # failure must never crash the worker or roll back the finished turn.
        try:
            session._deliver_result(rc == 0)
        except Exception:  # noqa: BLE001
            _logger.exception("AI assistant: result delivery failed")

    # ------------------------------------------------------------------
    # Native (in-process) execution — no subprocess, no stdout pump.
    # The adapter's ``_run_stream`` generator yields the same ops the CLI
    # parser would; we apply them with the identical machinery, plus a
    # turn deadline checked between ops.
    # ------------------------------------------------------------------
    def _execute_native(self, env, session, ctx):
        adapter = self._adapter(session)
        _logger.info("AI assistant: native run provider=%s (session %s)",
                     ctx["provider"], ctx["session_id"])
        self._emit(session, {"kind": "started"})
        env.cr.commit()

        deadline = time.time() + ctx["timeout_s"]
        # Expose the wall-clock deadline so native adapters (and the HTTP clients
        # they drive) can cap their own retry/backoff budget instead of blocking
        # past the turn timeout inside a single op.
        ctx["deadline"] = deadline
        next_typing = time.time() + 40  # refresh the Discuss "typing…" indicator
        msg_map = {}
        produced_text = {"any": False}
        try:
            for op in adapter._run_stream(env, session, ctx):
                if time.time() > deadline:
                    raise TimeoutError("Run exceeded timeout")
                if session.discuss_channel_id and time.time() > next_typing:
                    session._discuss_set_typing(True)
                    next_typing = time.time() + 40
                    env.cr.commit()
                if op:
                    self._apply_op(env, session, op, msg_map, produced_text)
                    env.cr.commit()
        finally:
            if ctx.get("routing_sid"):
                env["aidoo.session"]._delete_routing_sid(ctx["routing_sid"])

        self._persist_turn_artifacts(
            env, session, env["aidoo.message"].browse(list(msg_map.values())))
        session.write({"last_run_pid": False,
                       "turn_count": session.turn_count + 1})
        # An error op (yielded by the adapter) already set state="error" via
        # _apply_op; otherwise settle the turn as done.
        errored = session.state == "error"
        self._finalize_pending_tools(
            env, session, "error" if errored else "done", msg_map)
        if session.state == "running":
            session.write({"state": "done"})
            self._emit(session, {"kind": "done"})
        env.cr.commit()
        try:
            session._deliver_result(not errored)
        except Exception:  # noqa: BLE001
            _logger.exception("AI assistant: result delivery failed")

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

    def _persist_turn_artifacts(self, env, session, records):
        """Persist the turn's generated HTML blocks as artifact attachments and
        re-emit the updated messages so an open chat swaps its inline cards to
        the server-backed ones. Must never fail a finished turn."""
        try:
            for rec in records.exists()._persist_artifacts():
                self._emit_message(session, rec)
        except Exception:  # noqa: BLE001
            _logger.exception("AI assistant: artifact persistence failed")

    # ------------------------------------------------------------------
    # Bus
    # ------------------------------------------------------------------
    def _emit(self, session, payload):
        payload = dict(payload, session_id=session.id)
        session.user_id.partner_id._bus_send("aidoo", payload)

    def _emit_message(self, session, rec):
        self._emit(session, {"kind": "message", "message": rec._to_frontend()[0]})

    def _emit_notice(self, ctx, text):
        """Emit a transient ``notice`` bus event on its OWN committed cursor.

        A notice (e.g. "rate-limited, retrying in Ns…") must reach the client
        while the run worker is blocked in a backoff ``time.sleep`` — there is no
        commit on the worker's cursor during that wait, so we emit + commit on a
        separate short cursor (same pattern as the audit log). Best-effort: a
        failure here never disturbs the run. The notice is transient and not
        persisted as a message."""
        try:
            with Registry(ctx["dbname"]).cursor() as cr:
                env = api.Environment(cr, ctx["uid"], {})
                session = env["aidoo.session"].browse(ctx["session_id"])
                session.user_id.partner_id._bus_send(
                    "aidoo", {"kind": "notice", "text": text,
                              "session_id": session.id})
                cr.commit()
        except Exception:  # noqa: BLE001
            _logger.exception("AI assistant: notice emit failed")
