# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Gemini (Google Gemini CLI) provider adapter.

Drives the headless ``gemini`` CLI with stream-json output and a stdio MCP
bridge. Unlike Claude, Gemini has no --mcp-config / --settings / system-prompt
flags: the Odoo MCP server and the built-in tool surface come from a per-session
``<scratch>/.gemini/settings.json`` (loaded from the cwd workspace), the prompt
(SYSTEM + IDENTITY + history + user turn) is fed via STDIN, and the built-in
surface is narrowed with a ``tools.exclude`` list (an allowlist would also hide
the MCP tools under gemini-cli 0.46+). Per-user auth is either imported OAuth
``oauth_creds.json`` (Google login) or a Gemini API key.
"""
import json
import os

from odoo import _
from odoo.exceptions import UserError

from .base import AidooProvider, SYSTEM_PROMPT
from ..aidoo_session import WEB_TOOLS

# Built-in Gemini CLI tools the model is allowed to keep. We restrict the
# built-in surface to file READS (so it can open user attachments in the
# workspace) — everything else is withheld, leaving the gated Odoo MCP tools as
# the only way to touch the database. Web tools are added only when granted.
CORE_TOOLS_BASE = ["read_file", "read_many_files"]
# Gemini's built-in web tool names, mapped from our per-user web grants.
WEB_TOOL_BUILTINS = {"web_fetch": "web_fetch", "web_search": "google_web_search"}
# Every Gemini CLI built-in tool name. We restrict the built-in surface with an
# EXCLUDE list (the built-ins NOT in CORE_TOOLS_BASE / not granted) rather than a
# `tools.core` ALLOWLIST: in gemini-cli 0.46+ a core allowlist also hides every
# MCP tool, which makes the gated Odoo MCP tools invisible to the model. Keep
# this list current with the CLI's built-in tool set.
ALL_BUILTIN_TOOLS = [
    "run_shell_command", "write_file", "replace", "save_memory",
    "list_directory", "read_file", "read_many_files",
    "search_file_content", "glob", "web_fetch", "google_web_search",
    # gemini-cli 0.46 internal planning tool — withheld so its verbose
    # "topic/strategic_intent" calls don't clutter the chat transcript.
    "update_topic",
]


class AidooProviderGemini(AidooProvider):
    _name = "aidoo.provider.gemini"
    _inherit = "aidoo.provider"
    _description = "Aidoo Provider — Gemini"

    _provider_code = "gemini"
    _provider_label = "Gemini"
    _accent_color = "#714b67"
    _config_dir_name = ".gemini"
    _default_cli_glob = "/usr/local/bin/gemini"
    _default_model = ""
    _auth_ui_mode = "import_or_key"
    _uses_system_prompt_flag = False

    # ------------------------------------------------------------------
    # Runtime env: HOME + GEMINI_CLI_HOME (per user) + auth + workspace trust
    # ------------------------------------------------------------------
    def _oauth_env(self, user):
        """Point the CLI at THIS user's private home (where its .gemini config /
        credentials live) and merge the user's chosen authentication.

        Isolating HOME and GEMINI_CLI_HOME per user keeps credentials, config and
        session state fully separate, and lets the CLI refresh that user's OAuth
        token in place. OAuth import takes precedence: when imported creds exist
        we do NOT set GEMINI_API_KEY, so the CLI uses the OAuth path."""
        home = self._home_dir(user)
        # Ensure the config dir exists so the CLI can read/refresh creds there.
        self._config_dir(user, create=True)
        env = {
            "HOME": home,
            "GEMINI_CLI_HOME": home,  # -> <home>/.gemini
            # Trust the scratch workspace so the project .gemini/settings.json
            # loads without an interactive folder-trust prompt.
            "GEMINI_CLI_TRUST_WORKSPACE": "true",
        }
        if not self._has_oauth(user):
            key = self._api_key(user)
            if key:
                env["GEMINI_API_KEY"] = key
        project = (user.aidoo_cloud_project or "").strip() or (
            self._pconfig("cloud_project", "") or "")
        if project:
            env["GOOGLE_CLOUD_PROJECT"] = project
        return env

    # ------------------------------------------------------------------
    # Per-user credential files (under the .gemini config dir)
    # ------------------------------------------------------------------
    def _oauth_creds_path(self, user):
        return os.path.join(self._config_dir(user), "oauth_creds.json")

    def _api_key_path(self, user):
        """Per-user Gemini API key file (on disk, mode 0600, never the DB)."""
        return os.path.join(self._config_dir(user), "api_key")

    def _has_oauth(self, user):
        """True if a usable imported OAuth credential file is present."""
        try:
            with open(self._oauth_creds_path(user), "r") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        return bool(data.get("refresh_token") or data.get("access_token"))

    def _api_key(self, user):
        """The effective Gemini API key for this user: the per-user key if set,
        else the shared `aidoo.gemini.api_key` system parameter (may be empty)."""
        try:
            with open(self._api_key_path(user), "r") as f:
                key = f.read().strip()
            if key:
                return key
        except OSError:
            pass
        return self._pconfig("api_key", "") or ""

    def _is_authenticated(self, user):
        """True if this user can run the CLI: imported OAuth creds or an API key."""
        return bool(self._has_oauth(user) or self._api_key(user))

    # ------------------------------------------------------------------
    # Prompt assembly (no system-prompt flag — fold everything into stdin)
    # ------------------------------------------------------------------
    def _build_prompt(self, session, body, paths, is_first, identity):
        """Fold the system prompt, identity block, prior history (when not the
        first turn) and the user message into one stdin prompt.

        Gemini has no system-prompt flag, so the SYSTEM prompt leads the text;
        the identity block (which reflects the current tool grant) is included
        every turn; prior turns are replayed from history because Gemini headless
        does not reliably emit a resumable session id."""
        parts = [SYSTEM_PROMPT, identity]
        if not is_first:
            history = session._history_for_prompt()
            if history:
                parts.append("--- CONVERSATION SO FAR ---\n" + history)
        parts.append("--- USER MESSAGE ---\n" + session._compose_prompt(body, paths))
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Launch: write .gemini/settings.json, build argv/env; prompt via stdin
    # ------------------------------------------------------------------
    def _prepare_run(self, ctx):
        settings_path = self._write_settings(ctx)
        argv = self._build_argv(ctx)
        env = self._base_run_env(ctx)
        # Gemini reads settings.json from the cwd's .gemini dir.
        return {
            "argv": argv,
            "env": env,
            "cwd": ctx["scratch"],
            "stdin": ctx["prompt"],
            "cleanup": [settings_path],
        }

    def _build_argv(self, ctx):
        """Build the headless `gemini` argv for this turn.

        The MCP server and the built-in tool allowlist come from the per-session
        `.gemini/settings.json` in the scratch cwd (Gemini has no
        --mcp-config/--settings flags). The prompt is read from STDIN (no -p), so
        the SYSTEM/IDENTITY block can never be mistaken for a CLI flag.

        --approval-mode yolo auto-approves tool calls (no TTY to approve them in
        headless mode); combined with the MCP server's `trust: true` and the
        restricted built-in surface, the only capabilities the model has are the
        loopback-only, token-gated Odoo MCP tools plus reading the user's
        attachments in the workspace."""
        output_format = ctx.get("output_format") or "stream-json"
        argv = [ctx["cli_path"], "--output-format", output_format]
        if ctx["model"]:
            argv += ["--model", ctx["model"]]
        argv += ["--approval-mode", "yolo"]
        # Confine the built-in file tools to the per-session uploads dir (in
        # addition to the cwd workspace), so the model can open attachments.
        argv += ["--include-directories", ctx["uploads_dir"]]
        # Best-effort conversation continuity (Gemini headless does not always
        # emit a session id; the prompt history is the reliable fallback).
        if not ctx["is_first"] and ctx["cli_session_id"]:
            argv += ["--resume", ctx["cli_session_id"]]
        return argv

    def _write_settings(self, ctx):
        """Write `<scratch>/.gemini/settings.json` for this turn.

        Holds the Odoo MCP server definition and the built-in tool exclude list.
        Built-ins to withhold: everything the CLI ships except the file reads in
        CORE_TOOLS_BASE and any granted web tools. Expressed as an exclude list
        (not a `tools.core` allowlist) so the Odoo MCP tools stay visible under
        gemini-cli 0.46+."""
        web_builtins = [WEB_TOOL_BUILTINS[t]
                        for t in ctx["allowed_tools"] if t in WEB_TOOLS]
        allowed = set(CORE_TOOLS_BASE) | set(web_builtins)
        exclude = sorted(t for t in ALL_BUILTIN_TOOLS if t not in allowed)
        cfg = {
            "mcpServers": {
                "odoo": {
                    "command": ctx["python_bin"],
                    "args": [ctx["bridge_script"]],
                    "env": self._bridge_env(ctx),
                    "trust": True,
                },
            },
            # Nested form (current schema). Restrict the built-in surface to the
            # file-read tools (+ granted web tools); everything else is withheld.
            "tools": {"exclude": exclude},
            # Avoid an interactive folder-trust prompt for the scratch workspace.
            "security": {"folderTrust": {"enabled": False}},
        }
        gdir = os.path.join(ctx["scratch"], ".gemini")
        os.makedirs(gdir, mode=0o700, exist_ok=True)
        path = os.path.join(gdir, "settings.json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f)
        return path

    def _post_run(self, ctx, rc, stderr):
        """The streamed parser handles assistant text (including the trailing
        `result`/final-json `response`), so there is no on-disk fallback file to
        read here."""
        return None

    # ------------------------------------------------------------------
    # Stream parsing: Gemini stream-json (JSONL) -> common ops.
    # Defensive: gemini-cli has churned field names across versions, so each
    # extraction tries several aliases.
    # ------------------------------------------------------------------
    def _normalize_event(self, evt, state):
        etype = evt.get("type") or ""
        ops = []

        # init carries the Gemini session id used to resume later turns.
        if etype in ("init", "session", "system"):
            sid = (evt.get("session_id") or evt.get("sessionId")
                   or (evt.get("session") or {}).get("id")
                   or (evt.get("data") or {}).get("session_id"))
            if sid:
                ops.append({"op": "session_id", "id": sid})
            return ops

        if etype == "message":
            role = evt.get("role") or (evt.get("message") or {}).get("role")
            if role and role != "assistant":
                return ops
            text = self._event_text(evt)
            if not text:
                return ops
            is_delta = bool(evt.get("delta") or evt.get("subtype") == "delta")
            mid = (evt.get("id") or evt.get("message_id")
                   or (evt.get("message") or {}).get("id"))
            op = {"op": "text", "msg_id": mid, "text": text}
            # Gemini streams DELTAS; append them. A full (non-delta) message
            # replaces the accumulated text.
            if not is_delta:
                op["replace"] = True
            ops.append(op)
            return ops

        if etype in ("tool_use", "tool_call"):
            tid = (evt.get("id") or evt.get("tool_use_id")
                   or evt.get("call_id") or evt.get("callId")
                   or evt.get("tool_id"))  # gemini-cli 0.46 uses tool_id
            name = (evt.get("name") or evt.get("tool")
                    or evt.get("tool_name") or "tool")
            tool_input = (evt.get("input") or evt.get("args")
                          or evt.get("arguments") or evt.get("parameters") or {})
            ops.append({
                "op": "tool_use",
                "msg_id": (evt.get("message_id") or evt.get("msg_id")),
                "id": tid,
                "name": self._strip_tool_prefix(name),
                "input": tool_input if isinstance(tool_input, dict) else {},
            })
            return ops

        if etype == "tool_result":
            tid = (evt.get("tool_use_id") or evt.get("id")
                   or evt.get("call_id") or evt.get("callId")
                   or evt.get("tool_id"))  # gemini-cli 0.46 uses tool_id
            result = (evt.get("result") if evt.get("result") is not None
                      else evt.get("output"))
            if isinstance(result, (dict, list)):
                result = json.dumps(result, default=str)
            is_error = bool(evt.get("is_error") or evt.get("isError")
                            or evt.get("error") or evt.get("status") == "error")
            ops.append({
                "op": "tool_result", "id": tid,
                "text": str(result) if result is not None else "",
                "is_error": is_error,
            })
            return ops

        if etype == "result":
            if evt.get("error"):
                ops.append({"op": "error",
                            "text": self._error_text(evt.get("error"))})
                return ops
            text = evt.get("response") or self._event_text(evt)
            ops.append({"op": "done", "text": text or None})
            return ops

        if etype == "error":
            ops.append({"op": "error",
                        "text": self._error_text(evt.get("error") or evt)})
            return ops

        # Unknown event — never raise.
        return ops

    # ------------------------------------------------------------------
    # Defensive text/error extraction helpers (ported from geminoo).
    # ------------------------------------------------------------------
    @staticmethod
    def _event_text(evt):
        """Pull assistant text out of a Gemini event under any known key."""
        for key in ("delta", "text", "content", "response"):
            val = evt.get(key)
            if isinstance(val, str) and val:
                return val
        msg = evt.get("message")
        if isinstance(msg, dict):
            return AidooProviderGemini._content_text(msg.get("content"))
        if isinstance(msg, str):
            return msg
        return AidooProviderGemini._content_text(evt.get("content"))

    @staticmethod
    def _content_text(content):
        """Flatten a list-of-blocks content payload into plain text."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            out = []
            for b in content:
                if isinstance(b, dict):
                    out.append(b.get("text") or b.get("content") or "")
                elif isinstance(b, str):
                    out.append(b)
            return "".join(out)
        return ""

    @staticmethod
    def _error_text(err):
        if isinstance(err, dict):
            return (err.get("message") or err.get("error")
                    or json.dumps(err, default=str))
        return err if isinstance(err, str) else "Gemini run failed."

    # ------------------------------------------------------------------
    # Authentication — import OAuth creds blob OR set a Gemini API key
    # ------------------------------------------------------------------
    def _auth_import(self, user, blob):
        """Validate and store an imported Gemini OAuth credentials blob.

        ``blob`` is the JSON content of a ``~/.gemini/oauth_creds.json`` produced
        by `gemini` Google login on a machine with a browser. We persist it for
        this user; the CLI auto-refreshes the access token via the refresh_token.
        """
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
        for path in (self._oauth_creds_path(user), self._api_key_path(user)):
            try:
                os.remove(path)
            except OSError:
                pass
        return {"authenticated": False}
