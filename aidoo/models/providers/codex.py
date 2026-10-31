# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Codex (ChatGPT subscription) provider — NATIVE (in-process) adapter.

Like the native Gemini and Z.AI adapters, this provider does **not** drive a
CLI subprocess. It talks to the ChatGPT Codex backend — a Responses-API shaped
SSE endpoint at ``https://chatgpt.com/backend-api/codex`` — directly from
Python (see ``codex_client.py``) and runs the agent tool-calling loop
in-process, so the ``codex`` binary is not needed at all.

It is marked ``_is_native = True``; the runner drives ``_run_stream`` (a
generator of the same ops the CLI parsers emit) instead of spawning a process.
Tools are executed in-process through ``aidoo.tool.exec`` — the same
enforcement + audit path the loopback MCP bridge uses — so su=False and the
audit log are preserved.

Per-user auth is unchanged: the ChatGPT subscription device-code login writing
``$CODEX_HOME/auth.json`` — but the device flow is now reimplemented in pure
Python (``codex_client.device_code_start`` / ``device_code_poll_and_exchange``)
instead of shelling out to ``codex login``. An ``auth.json`` produced by
``codex login`` on another machine can also be pasted via credential import.

The backend is stateless (``store:false``): prior turns are replayed into the
``input`` list each request, and within a turn the completed output items
(including ``reasoning`` items with encrypted content) are carried forward
between tool rounds. The legacy subprocess adapter is preserved, unregistered,
in ``codex_cli.py``.
"""
import base64
import json
import logging
import mimetypes
import os
import threading
import uuid

from odoo import _
from odoo.exceptions import UserError

from .base import AidooProvider, SYSTEM_PROMPT, _bridge_tool_schemas
from .codex_client import (
    CHATGPT_CODEX_BASE,
    CodexAuthError,
    CodexClient,
    CodexError,
    device_code_poll_and_exchange,
    device_code_start,
)

_logger = logging.getLogger(__name__)

# In-flight device-login attempts, one per user: {user_id: threading.Event}.
# A new login (or a logout) sets the previous attempt's cancel event. Kept
# in-process only — a login is a short-lived UI interaction, and auth.json on
# disk is the durable outcome the status poll watches.
_LOGIN_CANCEL = {}
_LOGIN_LOCK = threading.Lock()


class AidooProviderCodex(AidooProvider):
    _name = "aidoo.provider.codex"
    _inherit = "aidoo.provider"
    _description = "Aidoo Provider — Codex (native ChatGPT backend)"

    _provider_code = "codex"
    _provider_label = "Codex"
    _accent_color = "#10a37f"          # codex used teal
    _provider_icon = "/aidoo/static/src/svg/openai.svg"
    _config_dir_name = ".codex"        # CODEX_HOME lives at <home>/.codex
    # The native client must always pin a model (the request body requires one;
    # there is no CLI "account default" to fall back to). NB: the ChatGPT
    # backend only serves the mainline chat models — codex-branded ids
    # (gpt-*-codex) are rejected with "not supported when using Codex with a
    # ChatGPT account"; those remain API-key-only on api.openai.com.
    _default_model = "gpt-5.5"
    _auth_ui_mode = "device_poll"
    _uses_system_prompt_flag = False
    _is_native = True

    # ------------------------------------------------------------------
    # Authentication state — read/write $CODEX_HOME/auth.json
    # ------------------------------------------------------------------
    def _auth_path(self, user):
        return os.path.join(self._config_dir(user), "auth.json")

    def _load_auth(self, user):
        """The parsed auth.json dict, or None if absent/unusable."""
        try:
            with open(self._auth_path(user), "r") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _is_authenticated(self, user):
        """True if this user has stored, non-empty Codex credentials.

        The exact schema is version-dependent; accept any of the known token
        locations (ChatGPT login nests under ``tokens``)."""
        data = self._load_auth(user)
        if not data:
            return False
        tokens = data.get("tokens") or {}
        return bool(
            (isinstance(tokens, dict) and tokens.get("access_token"))
            or data.get("access_token")
            or data.get("OPENAI_API_KEY"))

    def _base_url(self):
        """The Codex backend base URL, read PROVIDER-SCOPED only.

        We must not use ``_pconfig`` here: its fallback to the shared
        ``aidoo.base_url`` is the Odoo *host* URL, which would silently point
        the client at the local server. Read ``aidoo.codex.base_url`` directly
        and fall back to the ChatGPT backend constant."""
        val = self.env["ir.config_parameter"].sudo().get_param(
            "aidoo.codex.base_url")
        return val if val not in (None, False, "") else CHATGPT_CODEX_BASE

    # ------------------------------------------------------------------
    # Prompt: native assembles structured `input` items in _run_stream; here we
    # only need the composed current-turn user text (+ attachment manifest).
    # ------------------------------------------------------------------
    def _build_prompt(self, session, body, paths, is_first, identity):
        text = session._compose_prompt(body, paths)
        # Native Codex has no built-in file Read tool; it uses read_attachment.
        return text.replace("Use the Read tool to open them",
                            "Use the `read_attachment` tool (pass the full path) to open them"
                            ).replace("Read the .transcript.txt file",
                                      "Open the .transcript.txt file with `read_attachment`")

    # ------------------------------------------------------------------
    # The native turn: Responses SSE streaming + in-process tool loop
    # ------------------------------------------------------------------
    def _run_stream(self, env, session, ctx):
        user = session._credential_user().sudo()
        auth = self._load_auth(user)
        if not auth or not (auth.get("tokens") or {}).get("access_token"):
            yield {"op": "error", "text": _(
                "Connect Codex with your ChatGPT account to chat. Open the "
                "connect panel to sign in with a device code (or import your "
                "auth.json).")}
            return

        def _save(new_auth):
            try:
                self._write_user_file(
                    user, self._auth_path(user), json.dumps(new_auth, indent=2))
            except Exception:  # noqa: BLE001 — token persistence is best-effort
                # The refreshed token still works for this turn, but losing it
                # forces a re-auth once it expires, so leave a trace.
                _logger.warning(
                    "Codex: failed to persist refreshed OAuth tokens for user %s",
                    user.id, exc_info=True)

        runner = env["aidoo.runner"]

        def _on_retry(attempt, delay, reason):
            # Surface a live "retrying…" notice during the client's backoff
            # wait. Stay quiet for the first quick retry that recovers alone.
            if attempt < 2 and delay < 8:
                return
            if reason == 429:
                text = _("Codex is rate-limited — retrying in %ss…") % int(delay)
            else:
                text = _("Codex is busy — retrying in %ss…") % int(delay)
            runner._emit_notice(ctx, text)

        client = CodexClient(
            auth,
            save_auth=_save,
            base_url=self._base_url(),
            on_retry=_on_retry,
            rpm=int(self._pconfig("rpm_limit", 0) or 0),
            deadline=ctx.get("deadline"))
        # The runner resolves the model (persona override, else config default).
        model = ctx.get("model") or self._model()
        # Conversation id: client-minted, reused as the session_id header and
        # prompt_cache_key so the backend can cache the replayed prefix.
        sid = session.cli_session_id
        if not sid:
            sid = str(uuid.uuid4())
            yield {"op": "session_id", "id": sid}
        instructions = "%s\n\n%s" % (SYSTEM_PROMPT, ctx.get("identity") or "")
        uploads_dir = ctx.get("uploads_dir")
        has_uploads = bool(uploads_dir and os.path.isdir(uploads_dir)
                           and os.listdir(uploads_dir))
        if has_uploads:
            instructions += "\n\nAttachments are opened with `read_attachment`."
        input_items = self._build_input(session, ctx)
        tools = self._tool_specs(ctx.get("allowed_tools") or [])
        if has_uploads:
            tools = (tools or []) + [self._attachment_tool_spec()]
        # Reasoning summaries are a gpt-5-family feature; other models 400.
        reasoning = {"summary": "auto"} if (model or "").startswith("gpt-5") else None
        max_rounds = max(1, int(ctx.get("max_turns") or 30))

        rnd = 0
        tried_dev_fallback = False
        while rnd < max_rounds:
            msg_id = "x%s-%s" % (session.turn_count, rnd)
            calls = []
            failed = None
            try:
                stream = client.stream_responses(
                    model, input_items, instructions=instructions,
                    tools=tools, reasoning=reasoning,
                    session_id=sid, prompt_cache_key=sid)
                for evt in stream:
                    etype = evt.get("type") or ""
                    if etype == "response.output_text.delta":
                        piece = evt.get("delta") or ""
                        if piece:
                            yield {"op": "text", "msg_id": msg_id, "text": piece}
                    elif etype == "response.output_item.done":
                        item = evt.get("item") or {}
                        # Carry every completed item (message, function_call
                        # AND reasoning w/ encrypted content) into the next
                        # round's input — required for coherence with store:false.
                        input_items.append(item)
                        if item.get("type") == "function_call":
                            calls.append(item)
                    elif etype in ("response.failed", "response.incomplete"):
                        resp = evt.get("response") or {}
                        err = resp.get("error") or {}
                        failed = (err.get("message") if isinstance(err, dict)
                                  else str(err)) or _("Codex run failed.")
                    # response.completed / deltas of other kinds: nothing to do.
            except CodexAuthError as e:
                yield {"op": "error", "text": _(
                    "Codex authentication failed: %s") % e}
                return
            except CodexError as e:
                # Some models may reject a custom ``instructions`` text with a
                # 400. Retry ONCE (before anything streamed, so round 0 only)
                # with the system text demoted to a leading developer message.
                if (getattr(e, "status", None) == 400 and rnd == 0
                        and not tried_dev_fallback):
                    tried_dev_fallback = True
                    _logger.info(
                        "Codex: 400 on instructions, retrying with a "
                        "developer message: %.300s", e)
                    input_items.insert(0, {
                        "type": "message", "role": "developer",
                        "content": [{"type": "input_text",
                                     "text": instructions}]})
                    instructions = "You are a helpful assistant."
                    continue
                yield {"op": "error", "text": _("Codex request failed: %s") % e}
                return
            if failed:
                yield {"op": "error", "text": failed}
                return

            if not calls:
                return  # turn complete; the runner finalizes the 'done' state

            # Execute each requested tool in-process and feed results back.
            pending_images = []
            for item in calls:
                cid = item.get("call_id") or item.get("id") or ""
                name = self._strip_tool_prefix(item.get("name") or "")
                try:
                    args = json.loads(item.get("arguments") or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except ValueError:
                    _logger.warning(
                        "Codex: tool %s sent non-JSON arguments, using {}: %.200s",
                        name, item.get("arguments"))
                    args = {}
                yield {"op": "tool_use", "msg_id": msg_id, "id": cid,
                       "name": name, "input": args}
                if name == "read_attachment" and has_uploads:
                    result, image_item = self._read_attachment(
                        ctx, args.get("path"))
                    if image_item:
                        pending_images.append(image_item)
                else:
                    result = env["aidoo.tool.exec"]._dispatch(session, name, args)
                is_error = isinstance(result, dict) and set(result) == {"error"}
                payload = json.dumps(result, default=str, ensure_ascii=False)
                yield {"op": "tool_result", "id": cid,
                       "text": payload, "is_error": bool(is_error)}
                input_items.append({"type": "function_call_output",
                                    "call_id": cid, "output": payload})
            # Function outputs are text-only: show images as a user message.
            for image_item in pending_images:
                input_items.append({"type": "message", "role": "user",
                                    "content": [image_item]})
            rnd += 1

        yield {"op": "text", "msg_id": "x%s-cap" % session.turn_count,
               "text": _("\n\n_(stopped after %s tool rounds)_") % max_rounds}
        return  # hard stop: never run beyond the round cap

    # ------------------------------------------------------------------
    # Input / tool-schema assembly
    # ------------------------------------------------------------------
    def _build_input(self, session, ctx):
        """Responses ``input`` items: replayed prior user/assistant text, then
        this turn's user message. (The system text goes via ``instructions``.)

        The backend is stateless (store:false), so we replay recent context
        each turn — same windowing as the native Z.AI/Gemini adapters.

        On a first turn (``ctx['is_first']``) we skip the replay: there is no
        prior context in interactive chat, and scheduled runs force a first turn
        so the agent-configured context prefix (in ``ctx['prompt']``) is the
        sole, non-duplicated source of prior context."""
        items = []
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
                if m.role == "user":
                    role, ptype = "user", "input_text"
                else:
                    role, ptype = "assistant", "output_text"
                items.append({"type": "message", "role": role,
                              "content": [{"type": ptype, "text": body}]})
        items.append({"type": "message", "role": "user",
                      "content": [{"type": "input_text",
                                   "text": ctx.get("prompt") or ""}]})
        return items

    @staticmethod
    def _attachment_tool_spec():
        return {
            "type": "function",
            "name": "read_attachment",
            "description": (
                "Open a file the user attached to this chat. Text/CSV/code is "
                "returned verbatim, PDFs as extracted text, images are shown "
                "to you. Only files in the attachments directory can be read."),
            "strict": False,
            "parameters": {
                "type": "object",
                "properties": {"path": {
                    "type": "string",
                    "description": "Full path of the attachment, as listed."}},
                "required": ["path"],
            },
        }

    def _read_attachment(self, ctx, path):
        """Return ``(result, image_item)`` for the local read_attachment tool.
        Confined to ``ctx['uploads_dir']`` (realpath check)."""
        from ..aidoo_session import (
            UPLOAD_AUDIO_EXTS, UPLOAD_IMAGE_EXTS, UPLOAD_PDF_EXTS,
            UPLOAD_TEXT_EXTS)
        root = os.path.realpath(ctx.get("uploads_dir") or "")
        real = os.path.realpath(path) if isinstance(path, str) and path else ""
        if not real or not (real == root or real.startswith(root + os.sep)) \
                or not os.path.isfile(real):
            return {"error": "Not an attachment of this chat: %s" % path}, None
        name = os.path.basename(real).lower()
        ext = os.path.splitext(name)[1]
        limit = 200 * 1024
        try:
            if ext in UPLOAD_AUDIO_EXTS:
                return {"error": "Audio cannot be read; open the matching "
                                 ".transcript.txt file instead."}, None
            if ext in UPLOAD_TEXT_EXTS or name.endswith(".transcript.txt"):
                with open(real, "rb") as f:
                    data = f.read(limit + 1)
                text = data[:limit].decode("utf-8", errors="replace")
                if len(data) > limit:
                    text += "\n[truncated]"
                return {"content": text}, None
            if ext in UPLOAD_PDF_EXTS:
                try:
                    import PyPDF2
                except ImportError:
                    return {"error": "PDF reading is unavailable on this server."}, None
                reader = PyPDF2.PdfReader(real)
                parts, size = [], 0
                for i, page in enumerate(reader.pages, 1):
                    t = page.extract_text() or ""
                    parts.append("--- page %d ---\n%s" % (i, t))
                    size += len(t)
                    if size > limit:
                        break
                text = "\n".join(parts)
                if not text.replace("-", "").strip() or size == 0:
                    return {"error": "The PDF has no extractable text "
                                     "(probably scanned)."}, None
                if size > limit:
                    text = text[:limit] + "\n[truncated]"
                return {"content": text}, None
            if ext in UPLOAD_IMAGE_EXTS:
                if os.path.getsize(real) > 15 * 1024 * 1024:
                    return {"error": "Image too large."}, None
                with open(real, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                mime = mimetypes.guess_type(real)[0] or "image/png"
                return ({"content": "Image attached below as a user message."},
                        {"type": "input_image",
                         "image_url": "data:%s;base64,%s" % (mime, b64)})
        except Exception as e:  # noqa: BLE001 — report to the model, don't crash the turn
            return {"error": "Could not read attachment: %s" % e}, None
        return {"error": "Unsupported attachment type: %s" % ext}, None

    def _tool_specs(self, allowed_tools):
        """Responses-API ``tools`` for the user's granted Odoo tools.

        The Odoo tools come from the shared bridge schema, filtered to what the
        user is granted (the executor re-checks the grant authoritatively at
        call time). Unlike chat-completions (z.ai), Responses function tools
        are FLAT: ``name``/``parameters`` at the top level, no nested
        ``function`` object."""
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
                "name": t["name"],
                "description": t.get("description") or "",
                "strict": False,
                "parameters": t.get("inputSchema") or {"type": "object"},
            })
        return specs or None

    # ------------------------------------------------------------------
    # Authentication — ChatGPT device-code login, now in pure Python
    # ------------------------------------------------------------------
    def _cancel_login(self, user):
        """Signal any in-flight device-login poll thread for this user."""
        with _LOGIN_LOCK:
            ev = _LOGIN_CANCEL.pop(user.id, None)
        if ev is not None:
            ev.set()

    def _auth_start(self, user):
        """Begin a device-code login: obtain the verification URL + one-time
        code from auth.openai.com, then poll for approval in a background
        thread that writes ``auth.json`` when the exchange completes.

        Returns ``{"url", "code", "raw", "authenticated"}`` for the chat gate
        (same shape the CLI-scraping implementation returned, so the OWL
        device_poll panel is unchanged)."""
        if self._is_authenticated(user):
            return {"url": "", "code": "", "raw": "", "authenticated": True}

        self._cancel_login(user)  # cancel any previous in-flight attempt
        try:
            info = device_code_start()
        except CodexError as e:
            raise UserError(_(
                "Could not start the Codex device login: %s") % e)

        # Resolve everything the thread needs BEFORE spawning it: no self.env
        # (no cursor) may be touched from the worker thread.
        auth_path = self._auth_path(user)
        self._config_dir(user, create=True)
        cancel = threading.Event()
        with _LOGIN_LOCK:
            _LOGIN_CANCEL[user.id] = cancel

        def _worker():
            try:
                auth = device_code_poll_and_exchange(
                    info["device_auth_id"], info["user_code"],
                    interval=info["interval"], cancel_event=cancel)
            except CodexError as e:
                if not cancel.is_set():
                    _logger.warning("Codex device login failed: %s", e)
                return
            if cancel.is_set():
                return
            tmp = auth_path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(auth, f, indent=2)
            os.replace(tmp, auth_path)
            _logger.info("Codex device login completed for user %s", user.id)

        threading.Thread(target=_worker, daemon=True,
                         name="aidoo_codex_device_poll").start()
        return {"url": info["verify_url"], "code": info["user_code"],
                "raw": "", "authenticated": False}

    def _auth_status(self, user):
        """Are this user's credentials present? Drop the (finished) login
        attempt's cancel handle once authenticated."""
        ok = self._is_authenticated(user)
        if ok:
            with _LOGIN_LOCK:
                _LOGIN_CANCEL.pop(user.id, None)
        return {"authenticated": ok, "auth_mode": self._auth_ui_mode}

    def _auth_import(self, user, blob):
        """Validate and store a pasted ``auth.json`` (full file or just its
        ``tokens`` object), as produced by ``codex login`` on a machine with a
        browser. Mirrors the Gemini oauth_creds.json import."""
        blob = (blob or "").strip()
        if not blob:
            raise UserError(_("Paste the JSON content of your Codex auth.json."))
        try:
            data = json.loads(blob)
        except ValueError:
            raise UserError(_(
                "That is not valid JSON. Paste the full content of your "
                "~/.codex/auth.json file."))
        if not isinstance(data, dict):
            raise UserError(_("Paste the full content of your auth.json file."))
        # Accept either the whole file or a bare tokens object.
        tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) \
            else (data if data.get("access_token") else {})
        if not (tokens.get("access_token") and tokens.get("refresh_token")):
            raise UserError(_(
                "The pasted credentials do not contain an access_token and "
                "refresh_token. Run `codex login` on a machine with a browser, "
                "then paste ~/.codex/auth.json."))
        if not data.get("tokens"):
            data = {"OPENAI_API_KEY": None, "tokens": tokens}
        data.setdefault("last_refresh", None)
        self._write_user_file(
            user, self._auth_path(user), json.dumps(data, indent=2))
        return {"authenticated": True}

    def _auth_logout(self, user):
        """Remove this user's stored credentials (forces re-authentication)."""
        self._cancel_login(user)
        try:
            os.remove(self._auth_path(user))
        except OSError:
            pass
        return {"authenticated": False}
