# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Grok (xAI Grok CLI) provider adapter.

Drives the headless `grok` CLI in single-turn mode with streaming-json output.
The Odoo MCP server is configured through a per-turn ``.grok/config.toml`` written
into the scratch cwd; the prompt (system prompt + identity + history + user turn)
is delivered via ``--prompt-file`` so its leading ``---`` markers can never be
mistaken for CLI flags. Built-in tools are restricted with the ``--tools``
allowlist (file reads only, plus granted web tools); tool executions are
auto-approved with ``--always-approve`` (no TTY in headless mode). Per-user auth
is either an imported ``auth.json`` (from `grok login`) or an xAI API key
(XAI_API_KEY); the imported login takes precedence.
"""
import json
import os

from odoo import _
from odoo.exceptions import UserError

from .base import AidooProvider, SYSTEM_PROMPT
from ..aidoo_session import WEB_TOOLS

# Built-in Grok CLI tools the model is allowed to keep. We restrict the built-in
# surface to file READS (so it can open user attachments in the workspace) —
# everything else (run_terminal_cmd, write_file, edit, search_replace, glob,
# grep, list_dir, task, memory, web tools) is withheld, leaving the gated Odoo
# MCP tools as the only way to touch the database. Web tools are added here only
# when granted. `view_image` lets the model see attached images.
#
# Grok's `--tools` flag is an ALLOWLIST of *built-in* tools ("Only listed tools
# are available", headless-only). It does NOT touch MCP tools — those come from
# the `[mcp_servers.*]` block in the per-session .grok/config.toml the runner
# writes — so allowlisting only the read tools keeps the Odoo MCP tools fully
# available to the model. Tune the built-in names via the aidoo.grok.core_tools
# system parameter if a future CLI build renames them.
CORE_TOOLS_BASE = ["read_file", "view_image"]
# Grok's built-in web tool names, mapped from our per-user web grants. (These are
# Grok's own native built-in names, distinct from the Claude WebFetch/WebSearch
# mapping in aidoo_session.WEB_TOOL_BUILTINS.)
WEB_TOOL_BUILTINS = {"web_fetch": "web_fetch", "web_search": "web_search"}


class AidooProviderGrok(AidooProvider):
    _name = "aidoo.provider.grok"
    _inherit = "aidoo.provider"
    _description = "Aidoo Provider — Grok"

    _provider_code = "grok"
    _provider_label = "Grok"
    _accent_color = "#714b67"
    _provider_icon = "/aidoo/static/src/svg/grok.svg"
    _config_dir_name = ".grok"
    _default_cli_glob = "/usr/local/bin/grok"
    _default_model = ""
    _auth_ui_mode = "import_or_key"
    _uses_system_prompt_flag = False

    # ------------------------------------------------------------------
    # Runtime env: HOME (-> <home>/.grok holding auth.json) + auth (XAI_API_KEY)
    # ------------------------------------------------------------------
    def _oauth_env(self, user):
        """Env pointing the Grok CLI at THIS user's private home plus the user's
        chosen authentication.

        Each user's credentials/config live under ``<home>/.grok`` (the imported
        ``auth.json`` from `grok login` and/or an xAI API key). Isolating HOME per
        user keeps credentials, config and session state fully separate (Grok
        reads ``$HOME/.grok``) and lets the CLI refresh that user's OAuth token in
        place. The imported login takes precedence: if ``auth.json`` exists we do
        NOT set XAI_API_KEY, so the CLI uses the OAuth path."""
        env = {"HOME": self._home_dir(user)}
        if not self._has_oauth(user):
            key = self._api_key(user)
            if key:
                env["XAI_API_KEY"] = key
        return env

    # ------------------------------------------------------------------
    # Per-user credential files (under the user's .grok config dir)
    # ------------------------------------------------------------------
    def _auth_path(self, user):
        """Per-user ``auth.json`` produced by `grok login` (the OAuth path)."""
        return os.path.join(self._config_dir(user), "auth.json")

    def _api_key_path(self, user):
        """Per-user xAI API key file (kept on disk, mode 0600, never the DB)."""
        return os.path.join(self._config_dir(user), "api_key")

    def _has_oauth(self, user):
        """True if a usable imported ``auth.json`` (from `grok login`) is present."""
        try:
            with open(self._auth_path(user), "r") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return False
        # auth.json is an opaque token bundle written by `grok login`; treat any
        # non-empty JSON object as a usable credential and let the CLI validate.
        return isinstance(data, dict) and bool(data)

    def _api_key(self, user):
        """The effective xAI API key for this user: the per-user key if set,
        else the shared `aidoo.grok.api_key` system parameter (may be empty)."""
        try:
            with open(self._api_key_path(user), "r") as f:
                key = f.read().strip()
            if key:
                return key
        except OSError:
            pass
        return self._pconfig("api_key", "") or ""

    def _is_authenticated(self, user):
        """True if this user can run the CLI: imported login or an API key."""
        return bool(self._has_oauth(user) or self._api_key(user))

    # ------------------------------------------------------------------
    # Prompt assembly — Grok folds the system prompt + identity (and replayed
    # history) into the prompt text, delivered to the CLI via --prompt-file.
    # ------------------------------------------------------------------
    def _build_prompt(self, session, body, paths, is_first, identity):
        """Fold the system prompt, identity block, prior history (when not on the
        first turn) and the user message into a single prompt string.

        The full system prompt is included on the first turn or whenever we are
        not resuming a live Grok session; the identity block (which reflects the
        current tool grant) is included every turn. The returned string is written
        to the prompt file in ``_prepare_run``."""
        parts = []
        if is_first:
            parts.append(SYSTEM_PROMPT)
        parts.append(identity)
        if not is_first:
            history = session._history_for_prompt()
            if history:
                parts.append("--- CONVERSATION SO FAR ---\n" + history)
        parts.append("--- USER MESSAGE ---\n" + session._compose_prompt(body, paths))
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Launch: write .grok/config.toml + prompt file, build argv/env
    # ------------------------------------------------------------------
    def _prepare_run(self, ctx):
        config_path = self._write_config(ctx)
        prompt_path = self._write_prompt(ctx)
        argv = self._build_argv(ctx, prompt_path)
        env = self._base_run_env(ctx)
        # The real bridge token is forwarded to the stdio MCP child by Grok
        # through the process environment (kept out of any on-disk file).
        env["AI_BRIDGE_TOKEN"] = ctx["token"]
        return {
            "argv": argv,
            # Grok reads .grok/config.toml from the cwd.
            "cwd": ctx["scratch"],
            "env": env,
            "stdin": None,  # prompt is delivered via --prompt-file
            "cleanup": [config_path, prompt_path],
        }

    def _build_argv(self, ctx, prompt_path):
        """Build the headless `grok` argv for this turn.

        `--always-approve` auto-approves tool executions (there is no TTY to
        approve them in headless mode); combined with the built-in tool allowlist
        (`--tools`, file reads + granted web tools) the only capabilities the
        model has are the loopback-only, token-gated Odoo MCP tools plus reading
        the user's attachments. `--output-format` + `--prompt-file` put the CLI in
        single-turn headless mode; `-r <id>` resumes the prior Grok session when
        we have its id."""
        output_format = ctx.get("output_format") or "streaming-json"
        argv = [ctx["cli_path"], "--output-format", output_format]
        if ctx["model"]:
            argv += ["--model", ctx["model"]]
        argv += ["--always-approve"]
        # Allowlist of built-in tools (file reads + any granted web tools). MCP
        # tools are unaffected by --tools, so the Odoo tools stay available.
        core_tools = (self._pconfig("core_tools", "") or "").strip()
        if core_tools:
            base = [t.strip() for t in core_tools.split(",") if t.strip()]
        else:
            base = list(CORE_TOOLS_BASE)
        web_builtins = [WEB_TOOL_BUILTINS[t]
                        for t in ctx["allowed_tools"] if t in WEB_TOOLS]
        allowed_builtins = base + web_builtins
        argv += ["--tools", ",".join(allowed_builtins)]
        # Belt-and-suspenders: turn web search/fetch off unless explicitly granted
        # (they are also absent from the allowlist above).
        if not web_builtins:
            argv += ["--disable-web-search"]
        argv += ["--prompt-file", prompt_path]
        if not ctx["is_first"] and ctx["cli_session_id"]:
            argv += ["-r", ctx["cli_session_id"]]
        return argv

    def _write_config(self, ctx):
        """Write `<scratch>/.grok/config.toml` for this turn.

        Holds the Odoo MCP server definition (a stdio server: the bundled bridge
        script run under the configured Python). The bridge bearer token is NOT
        written to the file — it is passed through the process environment (see
        _prepare_run), which Grok forwards to the stdio MCP child process. This
        keeps the secret out of any on-disk file the model's read_file tool could
        open in the workspace."""
        bridge_env = self._bridge_env(ctx)
        lines = [
            "[mcp_servers.odoo]",
            "command = %s" % self._toml_str(ctx["python_bin"]),
            "args = [%s]" % self._toml_str(ctx["bridge_script"]),
            "enabled = true",
            "",
            "[mcp_servers.odoo.env]",
        ]
        for key in sorted(bridge_env):
            lines.append("%s = %s" % (key, self._toml_str(bridge_env[key])))
        toml = "\n".join(lines) + "\n"
        gdir = os.path.join(ctx["scratch"], ".grok")
        os.makedirs(gdir, mode=0o700, exist_ok=True)
        path = os.path.join(gdir, "config.toml")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(toml)
        return path

    @staticmethod
    def _toml_str(value):
        """Render a Python string as a TOML basic string (double-quoted)."""
        s = str(value or "")
        s = s.replace("\\", "\\\\").replace('"', '\\"')
        s = s.replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
        return '"%s"' % s

    def _write_prompt(self, ctx):
        """Write the composed prompt to a file passed via `--prompt-file`.

        Lives under the per-turn .grok dir and is removed after the run. It
        carries no secrets — only the system prompt, the identity block, any
        replayed history, and the user message."""
        gdir = os.path.join(ctx["scratch"], ".grok")
        os.makedirs(gdir, mode=0o700, exist_ok=True)
        path = os.path.join(gdir, "prompt.txt")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(ctx["prompt"])
        return path

    # ------------------------------------------------------------------
    # Fallback text for --output-format json (single final object).
    # ------------------------------------------------------------------
    def _post_run(self, ctx, rc, stderr):
        """Fallback when the CLI streamed no assistant message.

        The single-object `--output-format json` case is already handled inline by
        ``_normalize_event`` (it recognizes the trailing ``{text: ...}`` object and
        emits text/done ops). Nothing extra to recover here."""
        return None

    # ------------------------------------------------------------------
    # Stream parsing: Grok streaming-json (JSONL) -> common ops.
    #
    # Grok's streaming-json mirrors the Claude Code / Anthropic agent event
    # shape: a `system` (subtype=init) event carries the session id; `assistant`
    # events carry a `message` whose `content` is a list of blocks (text +
    # tool_use); `user` events carry tool_result blocks; a final `result`
    # (subtype=success) event closes the turn. We stay defensive about field
    # names so minor CLI revisions keep parsing. Unknown events -> [].
    # ------------------------------------------------------------------
    def _normalize_event(self, evt, state):
        if not isinstance(evt, dict):
            return []
        etype = evt.get("type") or ""
        ops = []

        # init / system carries the Grok session id used to resume later turns.
        if etype in ("system", "init", "session"):
            sid = self._session_id_of(evt)
            if sid:
                ops.append({"op": "session_id", "id": sid})
            return ops

        if etype == "assistant":
            return self._assistant_ops(evt)

        if etype == "user":
            # User-role events in the stream carry tool_result blocks.
            return self._user_ops(evt)

        # Some builds emit flat tool events too; handle them defensively.
        if etype in ("tool_use", "tool_call"):
            op = self._tool_use_op(evt)
            return [op] if op else []
        if etype == "tool_result":
            op = self._tool_result_op(evt)
            return [op] if op else []

        if etype == "result":
            sid = self._session_id_of(evt)
            if sid:
                ops.append({"op": "session_id", "id": sid})
            subtype = evt.get("subtype")
            if evt.get("is_error") or (subtype and subtype not in ("success",)):
                ops.append({"op": "error", "text": self._error_text(
                    evt.get("error") or evt.get("result") or subtype)})
                return ops
            text = evt.get("result")
            ops.append({"op": "done",
                        "text": text if isinstance(text, str) and text.strip()
                        else None})
            return ops

        if etype == "error":
            return [{"op": "error", "text": self._error_text(
                evt.get("error") or evt)}]

        # Non-streaming (`--output-format json`) mode: the CLI prints a single
        # final object (no recognized `type`) whose assistant text lives under
        # `text` (also accepts `result`/`response`). Emit it as text + done so the
        # turn closes with a message.
        if not etype:
            if evt.get("is_error") or evt.get("error"):
                return [{"op": "error", "text": self._error_text(
                    evt.get("error") or evt.get("text"))}]
            sid = self._session_id_of(evt)
            if sid:
                ops.append({"op": "session_id", "id": sid})
            text = (evt.get("text") or evt.get("result")
                    or evt.get("response") or "")
            if isinstance(text, str) and text.strip():
                ops.append({"op": "text", "msg_id": evt.get("id"), "text": text})
                ops.append({"op": "done", "text": None})
            return ops

        return []

    # --- streaming-json sub-parsers ----------------------------------
    @staticmethod
    def _session_id_of(evt):
        return (evt.get("session_id") or evt.get("sessionId")
                or (evt.get("session") or {}).get("id"))

    def _assistant_ops(self, evt):
        """An assistant event: a message whose content is text and tool_use
        blocks. Emit text (append) and tool_use ops."""
        msg = evt.get("message") if isinstance(evt.get("message"), dict) else evt
        cid = msg.get("id")
        content = msg.get("content")
        ops = []
        if isinstance(content, str):
            if content:
                ops.append({"op": "text", "msg_id": cid, "text": content})
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype in ("text", "output_text") or block.get("text"):
                    text = block.get("text") or block.get("content") or ""
                    if isinstance(text, str) and text:
                        ops.append({"op": "text", "msg_id": cid, "text": text})
                elif btype in ("tool_use", "tool_call"):
                    op = self._tool_use_op(block, msg_id=cid)
                    if op:
                        ops.append(op)
                # `thinking`/`reasoning` blocks are intentionally dropped.
        return ops

    def _user_ops(self, evt):
        """A user-role event in the stream carries tool_result blocks (the
        outcome of the Odoo MCP tool calls)."""
        msg = evt.get("message") if isinstance(evt.get("message"), dict) else evt
        content = msg.get("content")
        ops = []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    op = self._tool_result_op(block)
                    if op:
                        ops.append(op)
        return ops

    def _tool_use_op(self, evt, msg_id=None):
        item_id = (evt.get("id") or evt.get("tool_use_id")
                   or evt.get("tool_call_id") or evt.get("call_id")
                   or evt.get("callId") or evt.get("tool_id"))
        name = (evt.get("name") or evt.get("tool_name")
                or evt.get("tool") or "tool")
        return {
            "op": "tool_use",
            "msg_id": msg_id if msg_id is not None else evt.get("msg_id"),
            "id": item_id,
            "name": self._strip_tool_prefix(name),
            "input": (evt.get("input") or evt.get("arguments")
                      or evt.get("args") or evt.get("parameters") or {}),
        }

    def _tool_result_op(self, evt):
        item_id = (evt.get("tool_use_id") or evt.get("tool_call_id")
                   or evt.get("id") or evt.get("call_id") or evt.get("callId")
                   or evt.get("tool_id"))
        result = (evt.get("content") if evt.get("content") is not None
                  else evt.get("result") if evt.get("result") is not None
                  else evt.get("output"))
        result = self._flatten_result(result)
        is_error = bool(evt.get("is_error") or evt.get("isError")
                        or evt.get("error") or evt.get("status") == "error")
        return {
            "op": "tool_result",
            "id": item_id,
            "text": (str(result) if result is not None else ""),
            "is_error": is_error,
        }

    @staticmethod
    def _flatten_result(result):
        """Tool results may be a string, a dict, or a list of content blocks
        ([{type:text, text:...}]). Flatten to a string for display."""
        if isinstance(result, list):
            parts = []
            for b in result:
                if isinstance(b, dict):
                    parts.append(b.get("text") or b.get("content")
                                 or json.dumps(b, default=str))
                else:
                    parts.append(str(b))
            return "".join(parts)
        if isinstance(result, dict):
            return json.dumps(result, default=str)
        return result

    @staticmethod
    def _error_text(err):
        if isinstance(err, dict):
            return (err.get("message") or err.get("error")
                    or json.dumps(err, default=str))
        return err if isinstance(err, str) else "Grok run failed."

    # ------------------------------------------------------------------
    # Authentication — import_or_key (auth.json import OR xAI API key)
    # ------------------------------------------------------------------
    def _auth_import(self, user, blob):
        """Validate and store an imported Grok ``auth.json`` blob.

        ``blob`` is the JSON content of a ``~/.grok/auth.json`` produced by
        `grok login` on a machine with a browser (or `grok login --device-auth`
        on a headless box). We persist it for this user; the CLI refreshes the
        token in place."""
        blob = (blob or "").strip()
        if not blob:
            raise UserError(_("Paste your Grok auth.json content."))
        try:
            data = json.loads(blob)
        except ValueError:
            raise UserError(_("That is not valid JSON. Paste the full content of "
                              "your ~/.grok/auth.json file."))
        if not isinstance(data, dict) or not data:
            raise UserError(_(
                "The pasted content is not a valid Grok credential file. Run "
                "`grok login` (or `grok login --device-auth`) on a machine with a "
                "browser, then paste the contents of ~/.grok/auth.json."))
        self._write_user_file(user, self._auth_path(user), json.dumps(data))
        return {"authenticated": True}

    def _auth_set_key(self, user, key):
        """Store (or clear) this user's xAI API key on disk (mode 0600)."""
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
        for path in (self._auth_path(user), self._api_key_path(user)):
            try:
                os.remove(path)
            except OSError:
                pass
        return {"authenticated": False}
