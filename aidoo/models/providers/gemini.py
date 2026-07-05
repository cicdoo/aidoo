# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Gemini provider — NATIVE (in-process) Code Assist adapter.

Unlike the other providers, Gemini does **not** drive a CLI subprocess. The Node
``gemini`` CLI cannot complete HTTPS to Google in some egress environments (the
response is reset mid-body — ``ERR_STREAM_PREMATURE_CLOSE``), even though Python's
HTTP stack works fine there. So this adapter talks to the same backend the CLI
uses for Google-login ("subscription") auth — the Code Assist API at
``cloudcode-pa.googleapis.com`` — directly from Python (see ``codeassist.py``),
and runs the agent tool-calling loop in-process.

It is marked ``_is_native = True``; the runner drives ``_run_stream`` (a generator
of the same ops the CLI parsers emit) instead of spawning a process. Tools are
executed in-process through ``aidoo.tool.exec`` — the same enforcement + audit
path the loopback MCP bridge uses — so su=False and the audit log are preserved.

Auth UX is unchanged: the user imports their ``oauth_creds.json`` (Google login).
The legacy CLI adapter is preserved, unregistered, in ``gemini_cli.py``.
"""
import json
import logging
import os
import uuid

from odoo import _
from odoo.exceptions import UserError

from .base import AidooProvider, SYSTEM_PROMPT, _bridge_tool_schemas
from .codeassist import CodeAssistClient, CodeAssistError, DEFAULT_METADATA

_logger = logging.getLogger(__name__)


def _sanitize_schema(schema):
    """Convert an MCP JSON-Schema into the OpenAPI subset Gemini accepts.

    Keeps type/description/enum/format/properties/required/items; drops keys
    Gemini rejects (notably ``default``); collapses union ``type`` lists to a
    single type. Recurses into nested properties and array items."""
    if not isinstance(schema, dict):
        return {"type": "string"}
    out = {}
    for k in ("description", "enum", "format"):
        if k in schema:
            out[k] = schema[k]
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "string")
    if t:
        out["type"] = t
    if isinstance(schema.get("properties"), dict):
        out["type"] = "object"
        out["properties"] = {
            k: _sanitize_schema(v) for k, v in schema["properties"].items()}
        if schema.get("required"):
            out["required"] = list(schema["required"])
    if "items" in schema:
        out["type"] = "array"
        out["items"] = _sanitize_schema(schema["items"]) or {"type": "string"}
    # Gemini (notably gemini-2.5-flash) rejects an array property with no `items`
    # (Code Assist 400). Odoo's bare arrays — `domain`, `*args`, sql `params` —
    # hold heterogeneous/nested values the OpenAPI subset can't type, so expose
    # them as a JSON string the model fills; aidoo.tool.exec parses it back.
    if out.get("type") == "array" and "items" not in out:
        desc = (out.get("description") or "").strip()
        hint = ('Pass as a JSON array string, e.g. '
                '[["field", "operator", value]].')
        return {"type": "string", "description": (desc + " " + hint).strip()}
    out.setdefault("type", "object" if "properties" in out else "string")
    return out


class AidooProviderGemini(AidooProvider):
    _name = "aidoo.provider.gemini"
    _inherit = "aidoo.provider"
    _description = "Aidoo Provider — Gemini (native Code Assist)"

    _provider_code = "gemini"
    _provider_label = "Gemini"
    _accent_color = "#714b67"
    _provider_icon = "/aidoo/static/src/svg/gemini-color.svg"
    _config_dir_name = ".gemini"
    # Default to flash: on Code Assist the pro model has a much lower per-model
    # rate limit, so it 429s on almost every turn; flash has the headroom for a
    # smooth chat. Admins who want pro can set aidoo.gemini.model = gemini-2.5-pro
    # (and keep aidoo.gemini.fallback_model = gemini-2.5-flash for graceful 429s).
    _default_model = "gemini-2.5-flash"
    _auth_ui_mode = "import_or_key"
    _uses_system_prompt_flag = False
    _is_native = True

    # ------------------------------------------------------------------
    # Per-user credential / cache files (under the .gemini config dir)
    # ------------------------------------------------------------------
    def _oauth_creds_path(self, user):
        return os.path.join(self._config_dir(user), "oauth_creds.json")

    def _api_key_path(self, user):
        return os.path.join(self._config_dir(user), "api_key")

    def _project_path(self, user):
        """Cached Code Assist project id (so we skip onboarding each turn)."""
        return os.path.join(self._config_dir(user), "code_assist_project")

    def _load_oauth(self, user):
        """Return the imported OAuth creds dict, or None if absent/unusable."""
        try:
            with open(self._oauth_creds_path(user), "r") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        if isinstance(data, dict) and (
                data.get("refresh_token") or data.get("access_token")):
            return data
        return None

    def _has_oauth(self, user):
        return bool(self._load_oauth(user))

    def _api_key(self, user):
        try:
            with open(self._api_key_path(user), "r") as f:
                key = f.read().strip()
            if key:
                return key
        except OSError:
            pass
        return self._pconfig("api_key", "") or ""

    def _is_authenticated(self, user):
        """True if the user can run: imported OAuth creds or an API key."""
        return bool(self._has_oauth(user) or self._api_key(user))

    @staticmethod
    def _read_text(path):
        try:
            with open(path, "r") as f:
                return f.read().strip()
        except OSError:
            return ""

    # ------------------------------------------------------------------
    # Prompt: native assembles structured `contents` in _run_stream; here we
    # only need the composed current-turn user text (+ attachment manifest).
    # ------------------------------------------------------------------
    def _build_prompt(self, session, body, paths, is_first, identity):
        return session._compose_prompt(body, paths)

    # ------------------------------------------------------------------
    # The native turn: Code Assist streaming + in-process tool loop
    # ------------------------------------------------------------------
    def _run_stream(self, env, session, ctx):
        user = session._credential_user().sudo()
        creds = self._load_oauth(user)
        if not creds:
            yield {"op": "error", "text": _(
                "Connect Gemini with your Google login (import your "
                "oauth_creds.json) to chat. API-key auth is not yet supported by "
                "the native Gemini adapter.")}
            return

        def _save(new_creds):
            try:
                self._write_user_file(
                    user, self._oauth_creds_path(user), json.dumps(new_creds))
            except Exception:  # noqa: BLE001 — token persistence is best-effort
                # Best-effort: the refreshed token still works for this turn, but
                # losing it forces a re-auth next turn, so leave a trace.
                _logger.warning(
                    "Gemini: failed to persist refreshed OAuth creds for user %s",
                    user.id, exc_info=True)

        # Code Assist project resolution. A CONFIGURED project (the user's own
        # aidoo_cloud_project, else the shared aidoo.gemini.cloud_project) is
        # authoritative — required to use a Gemini Code Assist *Enterprise* tier,
        # which is granted on a specific GCP project + per-account license. With
        # no configured project we fall back to the cached/auto-onboarded one
        # (Google's individual/standard-tier managed project).
        configured_project = ((user.aidoo_cloud_project or "").strip()
                              or (self._pconfig("cloud_project", "") or "").strip())
        cached_project = self._read_text(self._project_path(user))
        init_project = configured_project or cached_project or None
        runner = env["aidoo.runner"]

        def _on_retry(attempt, delay, reason):
            # Surface a live "retrying…" notice during the client's backoff wait
            # (emitted on its own committed cursor, so it reaches the chat even
            # while this worker is blocked in time.sleep). Stay quiet for the
            # first quick retry that recovers on its own; only speak up for a
            # longer wait or a later attempt, so the chat isn't noisy.
            if attempt < 2 and delay < 8:
                return
            if reason == 429:
                text = _("Gemini is rate-limited — retrying in %ss…") % int(delay)
            else:
                text = _("Gemini is busy — retrying in %ss…") % int(delay)
            runner._emit_notice(ctx, text)

        rpm = int(self._pconfig("rpm_limit", 120) or 0)
        client = CodeAssistClient(
            creds, save_creds=_save, project=init_project,
            on_retry=_on_retry, rpm=rpm, deadline=ctx.get("deadline"))
        model = self._model()
        # Lighter model to fall back to when the primary stays rate-limited (a
        # separate quota bucket). Empty disables fallback; same-as-primary is a
        # no-op. Mirrors the gemini CLI's onPersistent429 -> flash behaviour.
        fallback_model = (self._pconfig("fallback_model", "gemini-2.5-flash")
                          or "").strip()
        fallback_used = False

        try:
            project = client.ensure_project(DEFAULT_METADATA)
        except CodeAssistError as e:
            yield {"op": "error", "text": _("Gemini authentication failed: %s") % e}
            return
        # Cache only an AUTO-resolved project; a configured one is authoritative
        # and must not be overwritten by/stored as a stale cache.
        if (not configured_project and project and project != cached_project):
            self._write_user_file(user, self._project_path(user), project)

        system_instruction = "%s\n\n%s" % (SYSTEM_PROMPT, ctx.get("identity") or "")
        tools = self._function_declarations(ctx.get("allowed_tools") or [])
        contents = self._build_contents(session, ctx)
        max_rounds = max(1, int(ctx.get("max_turns") or 30))

        for rnd in range(max_rounds):
            msg_id = "g%s-%s" % (session.turn_count, rnd)
            # Stream one model round, with a one-shot fallback to the lighter
            # model if the primary is still rate-limited after the client's
            # own backoff. Re-running a round is only safe before any text has
            # streamed for it (so we never duplicate output). Bounded to the
            # primary attempt plus a single fallback retry — never an open loop.
            for _attempt in range(2):
                upid = uuid.uuid4().hex
                model_parts = []
                func_calls = []
                produced = False
                try:
                    for resp in client.stream_generate(
                            model, contents,
                            system_instruction=system_instruction,
                            tools=tools, user_prompt_id=upid):
                        for part in CodeAssistClient.iter_parts(resp):
                            if part.get("functionCall"):
                                model_parts.append(part)
                                func_calls.append(part["functionCall"])
                            elif part.get("text") and not part.get("thought"):
                                model_parts.append(part)
                                produced = True
                                yield {"op": "text", "msg_id": msg_id,
                                       "text": part["text"]}
                    break  # round streamed successfully
                except CodeAssistError as e:
                    # Fall back to the lighter model when the primary is either
                    # rate-limited (429) or unavailable on this tier (404). Only
                    # safe before any text streamed this round (no duplication).
                    if ((e.is_rate_limit or e.is_not_found) and fallback_model
                            and fallback_model != model
                            and not fallback_used and not produced):
                        fallback_used = True
                        if e.is_not_found:
                            note = _("Model %(bad)s is unavailable — using "
                                     "%(fb)s instead…") % {
                                "bad": model, "fb": fallback_model}
                        else:
                            note = _("Gemini rate-limited — switching to %s for "
                                     "this reply…") % fallback_model
                        model = fallback_model
                        runner._emit_notice(ctx, note)
                        continue  # retry this round on the fallback model
                    yield {"op": "error",
                           "text": _("Gemini request failed: %s") % e}
                    return
            else:
                # Both attempts were consumed by retries without a clean stream —
                # defensive backstop so the bounded loop can never fall through
                # silently into the tool-handling code below.
                yield {"op": "error", "text": _(
                    "Gemini request failed after retrying on the fallback model.")}
                return

            if not func_calls:
                return  # turn complete; the runner finalizes the 'done' state

            # Record the model's turn (text + functionCalls) for continuity.
            contents.append({"role": "model", "parts": model_parts})

            # Execute each requested tool in-process and feed results back.
            resp_parts = []
            for idx, fc in enumerate(func_calls):
                name = fc.get("name") or ""
                args = fc.get("args") if isinstance(fc.get("args"), dict) else {}
                tid = "%s-%s" % (msg_id, idx)
                yield {"op": "tool_use", "msg_id": msg_id, "id": tid,
                       "name": name, "input": args}
                result = env["aidoo.tool.exec"]._dispatch(session, name, args)
                is_error = isinstance(result, dict) and set(result) == {"error"}
                yield {"op": "tool_result", "id": tid,
                       "text": json.dumps(result, default=str, ensure_ascii=False),
                       "is_error": bool(is_error)}
                resp_parts.append({"functionResponse": {
                    "name": name,
                    "response": result if isinstance(result, dict)
                    else {"result": result}}})
            contents.append({"role": "user", "parts": resp_parts})

        yield {"op": "text", "msg_id": "g%s-cap" % session.turn_count,
               "text": _("\n\n_(stopped after %s tool rounds)_") % max_rounds}
        return  # hard stop: never run beyond the round cap

    # ------------------------------------------------------------------
    # Contents / tool-schema assembly
    # ------------------------------------------------------------------
    def _build_contents(self, session, ctx):
        """Replay prior user/assistant text as structured Vertex contents, then
        append this turn's user message. Code Assist does not expose a resumable
        server session for headless turns, so context is replayed each turn.

        On a first turn (``ctx['is_first']``) we skip the replay: there is no
        prior context in interactive chat, and scheduled runs force a first turn
        so the agent-configured context prefix (in ``ctx['prompt']``) is the sole,
        non-duplicated source of prior context."""
        contents = []
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
                role = "user" if m.role == "user" else "model"
                contents.append({"role": role, "parts": [{"text": body}]})
        contents.append({
            "role": "user", "parts": [{"text": ctx.get("prompt") or ""}]})
        return contents

    def _function_declarations(self, allowed_tools):
        """Gemini functionDeclarations for the user's granted Odoo tools.

        Web/file built-ins are CLI-only and not offered here; the Odoo tools come
        from the shared bridge schema and are filtered to what the user is granted
        (the executor re-checks the grant authoritatively at call time)."""
        try:
            schemas = _bridge_tool_schemas()
        except Exception:  # noqa: BLE001 — never let schema loading break a run
            schemas = []
        allowed = set(allowed_tools)
        decls = []
        for t in schemas:
            if t.get("name") not in allowed:
                continue
            decls.append({
                "name": t["name"],
                "description": t.get("description") or "",
                "parameters": _sanitize_schema(
                    t.get("inputSchema") or {"type": "object"}),
            })
        return [{"functionDeclarations": decls}] if decls else None

    # ------------------------------------------------------------------
    # Authentication — import OAuth creds blob OR set a Gemini API key
    # ------------------------------------------------------------------
    def _auth_import(self, user, blob):
        """Validate and store an imported Gemini OAuth credentials blob.

        ``blob`` is the JSON content of a ``~/.gemini/oauth_creds.json`` produced
        by ``gemini`` Google login on a machine with a browser. The access token
        is auto-refreshed via the refresh_token on each turn."""
        blob = (blob or "").strip()
        if not blob:
            raise UserError(_("Paste your Gemini oauth_creds.json content."))
        try:
            data = json.loads(blob)
        except ValueError:
            raise UserError(_("That is not valid JSON. Paste the full content of "
                              "your ~/.gemini/oauth_creds.json file."))
        if not isinstance(data, dict) or not (
                data.get("refresh_token") or data.get("access_token")):
            raise UserError(_(
                "The pasted credentials do not contain an access_token or "
                "refresh_token. Run `gemini` and sign in with Google on a "
                "machine with a browser, then paste ~/.gemini/oauth_creds.json."))
        self._write_user_file(
            user, self._oauth_creds_path(user), json.dumps(data))
        # Drop any stale cached project so the next turn re-resolves it.
        try:
            os.remove(self._project_path(user))
        except OSError:
            pass
        return {"authenticated": True}

    def _auth_set_key(self, user, key):
        """Store (or clear) this user's Gemini API key on disk (mode 0600)."""
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

    def _auth_logout(self, user):
        """Remove this user's stored credentials (forces re-connection)."""
        for path in (self._oauth_creds_path(user), self._api_key_path(user),
                     self._project_path(user)):
            try:
                os.remove(path)
            except OSError:
                pass
        return {"authenticated": False}
