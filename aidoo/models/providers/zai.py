# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Z.AI (Zhipu GLM Coding Plan) provider — NATIVE (in-process) adapter.

Like the native Gemini adapter, this provider does **not** drive a CLI
subprocess. It talks to z.ai's GLM Coding Plan endpoint — an OpenAI-compatible
chat-completions API at ``https://api.z.ai/api/coding/paas/v4`` — directly from
Python (see ``zai_client.py``) and runs the agent tool-calling loop in-process.

It is marked ``_is_native = True``; the runner drives ``_run_stream`` (a generator
of the same ops the CLI parsers emit) instead of spawning a process. Tools are
executed in-process through ``aidoo.tool.exec`` — the same enforcement + audit
path the loopback MCP bridge uses — so su=False and the audit log are preserved.

Per-user auth is a z.ai API key (stored mode 0600 under the ``.zai`` config dir,
never the DB), with an optional shared fallback key (``aidoo.zai.api_key``).
"""
import json
import logging
import os

from odoo import _
from odoo.exceptions import UserError

from .base import AidooProvider, SYSTEM_PROMPT, _bridge_tool_schemas
from .zai_client import ZaiClient, ZaiError, ZAI_CODING_BASE

_logger = logging.getLogger(__name__)


class AidooProviderZai(AidooProvider):
    _name = "aidoo.provider.zai"
    _inherit = "aidoo.provider"
    _description = "Aidoo Provider — Z.AI (native GLM Coding Plan)"

    _provider_code = "zai"
    _provider_label = "Z.AI (GLM)"
    _accent_color = "#3859ff"
    _provider_icon = "/aidoo/static/src/svg/zai.svg"
    _config_dir_name = ".zai"
    _default_model = "glm-4.6"
    _auth_ui_mode = "import_or_key"
    _auth_key_supported = True
    _auth_import_help = (
        "Paste your z.ai API key in the API-key field below "
        "(from https://z.ai/manage-apikey/apikey-list).")
    _uses_system_prompt_flag = False
    _is_native = True

    # ------------------------------------------------------------------
    # Per-user credential file (under the .zai config dir)
    # ------------------------------------------------------------------
    def _api_key_path(self, user):
        return os.path.join(self._config_dir(user), "api_key")

    def _api_key(self, user):
        """The effective z.ai API key: the per-user key if set, else the shared
        ``aidoo.zai.api_key`` system parameter (may be empty)."""
        try:
            with open(self._api_key_path(user), "r") as f:
                key = f.read().strip()
            if key:
                return key
        except OSError:
            pass
        return self._pconfig("api_key", "") or ""

    def _is_authenticated(self, user):
        return bool(self._api_key(user))

    def _base_url(self):
        """The coding endpoint base URL, read PROVIDER-SCOPED only.

        We must not use ``_pconfig`` here: its fallback to the shared
        ``aidoo.base_url`` is the Odoo *host* URL, which would silently point the
        client at the local server. Read ``aidoo.zai.base_url`` directly and fall
        back to the z.ai coding endpoint constant."""
        val = self.env["ir.config_parameter"].sudo().get_param(
            "aidoo.zai.base_url")
        return val if val not in (None, False, "") else ZAI_CODING_BASE

    # ------------------------------------------------------------------
    # Prompt: native assembles structured `messages` in _run_stream; here we
    # only need the composed current-turn user text (+ attachment manifest).
    # ------------------------------------------------------------------
    def _build_prompt(self, session, body, paths, is_first, identity):
        return session._compose_prompt(body, paths)

    # ------------------------------------------------------------------
    # The native turn: z.ai streaming + in-process tool loop
    # ------------------------------------------------------------------
    def _run_stream(self, env, session, ctx):
        user = session._credential_user().sudo()
        key = self._api_key(user)
        if not key:
            yield {"op": "error", "text": _(
                "Connect z.ai with your API key to chat. Open the connect panel "
                "and paste your z.ai API key.")}
            return

        runner = env["aidoo.runner"]

        def _on_retry(attempt, delay, reason):
            # Surface a live "retrying…" notice during the client's backoff wait.
            # Stay quiet for the first quick retry that recovers on its own.
            if attempt < 2 and delay < 8:
                return
            if reason == 429:
                text = _("z.ai is rate-limited — retrying in %ss…") % int(delay)
            else:
                text = _("z.ai is busy — retrying in %ss…") % int(delay)
            runner._emit_notice(ctx, text)

        client = ZaiClient(
            key,
            base_url=self._base_url(),
            on_retry=_on_retry,
            rpm=int(self._pconfig("rpm_limit", 0) or 0),
            deadline=ctx.get("deadline"))
        model = self._model()
        messages = self._build_messages(session, ctx)
        tools = self._tool_specs(ctx.get("allowed_tools") or [])
        max_rounds = max(1, int(ctx.get("max_turns") or 30))

        for rnd in range(max_rounds):
            msg_id = "z%s-%s" % (session.turn_count, rnd)
            content_buf = ""
            tool_acc = {}
            try:
                for chunk in client.stream_chat(model, messages, tools=tools):
                    choice = (chunk.get("choices") or [{}])[0] or {}
                    delta = choice.get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        content_buf += piece
                        yield {"op": "text", "msg_id": msg_id, "text": piece}
                    if delta.get("tool_calls"):
                        ZaiClient.accumulate_tool_calls(
                            tool_acc, delta["tool_calls"])
            except ZaiError as e:
                yield {"op": "error", "text": _("z.ai request failed: %s") % e}
                return

            calls = [tool_acc[i] for i in sorted(tool_acc) if tool_acc[i].get("name")]
            if not calls:
                return  # turn complete; the runner finalizes the 'done' state

            # Record the model's turn (text + tool_calls) for continuity.
            messages.append({
                "role": "assistant",
                "content": content_buf or None,
                "tool_calls": [{
                    "id": c["id"], "type": "function",
                    "function": {"name": c["name"],
                                 "arguments": c["arguments"] or "{}"},
                } for c in calls],
            })

            # Execute each requested tool in-process and feed results back.
            for c in calls:
                try:
                    args = json.loads(c["arguments"] or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except ValueError:
                    _logger.warning(
                        "z.ai: tool %s sent non-JSON arguments, using {}: %.200s",
                        c.get("name"), c.get("arguments"))
                    args = {}
                yield {"op": "tool_use", "msg_id": msg_id, "id": c["id"],
                       "name": c["name"], "input": args}
                result = env["aidoo.tool.exec"]._dispatch(session, c["name"], args)
                is_error = isinstance(result, dict) and set(result) == {"error"}
                payload = json.dumps(result, default=str, ensure_ascii=False)
                yield {"op": "tool_result", "id": c["id"],
                       "text": payload, "is_error": bool(is_error)}
                messages.append({"role": "tool", "tool_call_id": c["id"],
                                 "content": payload})

        yield {"op": "text", "msg_id": "z%s-cap" % session.turn_count,
               "text": _("\n\n_(stopped after %s tool rounds)_") % max_rounds}
        return  # hard stop: never run beyond the round cap

    # ------------------------------------------------------------------
    # Messages / tool-schema assembly
    # ------------------------------------------------------------------
    def _build_messages(self, session, ctx):
        """OpenAI ``messages``: a system message (system prompt + identity),
        replayed prior user/assistant text, then this turn's user message.

        z.ai's coding endpoint is stateless for headless turns, so we replay
        recent context each turn (same windowing as the native Gemini adapter).

        On a first turn (``ctx['is_first']``) we skip the replay: there is no
        prior context in interactive chat, and scheduled runs force a first turn
        so the agent-configured context prefix (in ``ctx['prompt']``) is the sole,
        non-duplicated source of prior context."""
        messages = [{
            "role": "system",
            "content": "%s\n\n%s" % (SYSTEM_PROMPT, ctx.get("identity") or ""),
        }]
        if not ctx.get("is_first"):
            msgs = session.message_ids.filtered(
                lambda m: m.role in ("user", "assistant")).sorted("id")
            # The current turn's user message was just persisted (it is the last
            # one); replay only PRIOR turns and add this turn from ctx['prompt'].
            prior = msgs[:-1] if msgs else msgs
            for m in prior[-20:]:
                body = m.body or ""
                if not body:
                    continue
                role = "user" if m.role == "user" else "assistant"
                messages.append({"role": role, "content": body})
        messages.append({"role": "user", "content": ctx.get("prompt") or ""})
        return messages

    def _tool_specs(self, allowed_tools):
        """OpenAI ``tools`` for the user's granted Odoo tools.

        The Odoo tools come from the shared bridge schema, filtered to what the
        user is granted (the executor re-checks the grant authoritatively at call
        time). GLM follows OpenAI function-calling and accepts standard JSON
        Schema, so the bridge ``inputSchema`` is passed through unchanged."""
        try:
            schemas = _bridge_tool_schemas()
        except Exception:  # noqa: BLE001 — never let schema loading break a run
            schemas = []
        allowed = set(allowed_tools)
        specs = []
        for t in schemas:
            if t.get("name") not in allowed:
                continue
            specs.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description") or "",
                    "parameters": t.get("inputSchema") or {"type": "object"},
                },
            })
        return specs or None

    # ------------------------------------------------------------------
    # Authentication — set (or clear) a z.ai API key
    # ------------------------------------------------------------------
    def _auth_set_key(self, user, key):
        """Store (or clear) this user's z.ai API key on disk (mode 0600)."""
        key = (key or "").strip()
        path = self._api_key_path(user)
        if not key:
            try:
                os.remove(path)
            except OSError:
                pass
        else:
            self._write_user_file(user, path, key)
        return {"authenticated": self._is_authenticated(user)}

    def _auth_import(self, user, blob):
        """z.ai uses a plain API key, not a credentials file — route a pasted
        blob to the key store so the import box still works if a user uses it."""
        blob = (blob or "").strip()
        if not blob:
            raise UserError(_("Paste your z.ai API key."))
        return self._auth_set_key(user, blob)

    def _auth_logout(self, user):
        """Remove this user's stored key (forces re-connection)."""
        try:
            os.remove(self._api_key_path(user))
        except OSError:
            pass
        return {"authenticated": False}
