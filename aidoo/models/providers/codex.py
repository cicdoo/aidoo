# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Codex (OpenAI Codex CLI) provider adapter.

Drives the Codex CLI headless via ``codex exec --json`` (JSONL stream).  Codex
``exec`` has no ``--append-system-prompt`` flag, so the system prompt and the
per-user identity block are folded into the prompt text (fed via stdin).  The
Odoo MCP bridge is injected per-invocation with inline ``-c`` TOML overrides so
the per-session bridge token never touches a shared config file.

Per-user auth is the ChatGPT subscription device-code login: the user opens a
verification URL, enters the one-time code, and Codex completes the exchange in
the background and writes ``$CODEX_HOME/auth.json``.
"""
import json
import logging
import os
import queue
import re
import signal
import subprocess
import threading
import time

from odoo import _
from odoo.exceptions import UserError

from .base import AidooProvider, SYSTEM_PROMPT

_logger = logging.getLogger(__name__)

# Per-user, in-flight `codex login --device-auth` process id (so a new login can
# reap a stale one). Stored in ir.config_parameter, cleared on logout/complete.
_LOGIN_PID = "aidoo.codex.login_pid.%s"

# How long the start route waits to capture the device URL + code from the
# codex login subprocess before returning to the UI.
_LOGIN_CAPTURE_S = 15

# Patterns to lift the verification URL and one-time code out of the codex login
# stdout. The exact wording is Codex-version dependent; these are deliberately
# loose and we also return the raw text as a fallback.
_URL_RE = re.compile(r"https://\S*device\S*", re.IGNORECASE)
# Codex device codes vary in length (e.g. the 4-then-5 "IL70-LNADU"); accept any
# reasonable XXXX-XXXX grouping rather than a fixed 4-4 so the code is extracted
# (which also lets the capture loop exit early instead of stalling the full 15s).
_CODE_RE = re.compile(r"\b([A-Z0-9]{3,8}-[A-Z0-9]{3,8})\b")
# Strip ANSI color/format escapes the CLI emits, so the captured URL/code regexes
# match cleanly and any raw fallback shown in the UI is plain text.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _toml_str(value):
    """Quote a Python string as a TOML basic string for a -c override."""
    s = str(value)
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\n", "\\n").replace("\t", "\\t")
    return '"%s"' % s


def _toml_inline_table(mapping):
    """Render a flat {str: str} mapping as a TOML inline table."""
    items = ", ".join(
        "%s = %s" % (k, _toml_str(v)) for k, v in mapping.items())
    return "{ %s }" % items


class AidooProviderCodex(AidooProvider):
    _name = "aidoo.provider.codex"
    _inherit = "aidoo.provider"
    _description = "Aidoo Provider — Codex"

    _provider_code = "codex"
    _provider_label = "Codex"
    _accent_color = "#10a37f"          # codex used teal
    _provider_icon = "fa-terminal"
    _config_dir_name = ".codex"        # CODEX_HOME lives at <home>/.codex
    _default_cli_glob = "/usr/local/bin/codex"
    _default_model = ""                 # empty = ChatGPT account default
    _auth_ui_mode = "device_poll"
    _uses_system_prompt_flag = False

    # ------------------------------------------------------------------
    # Runtime env: HOME + CODEX_HOME (per user); force subscription auth
    # ------------------------------------------------------------------
    def _oauth_env(self, user):
        """Point the Codex CLI at THIS user's private home/config dir.

        Each user authenticates with their own ChatGPT subscription; Codex
        stores credentials in ``$CODEX_HOME/auth.json``. Isolating CODEX_HOME
        (and HOME) per user keeps credentials, config and session state fully
        separate. ``CODEX_API_KEY=""`` forces subscription auth (never an API
        key)."""
        return {
            "HOME": self._home_dir(user),
            "CODEX_HOME": self._config_dir(user, create=True),
            "CODEX_API_KEY": "",
        }

    # ------------------------------------------------------------------
    # Authentication state — read $CODEX_HOME/auth.json
    # ------------------------------------------------------------------
    def _auth_path(self, user):
        return os.path.join(self._config_dir(user), "auth.json")

    def _is_authenticated(self, user):
        """True if this user has stored, non-empty Codex credentials.

        Codex writes ``$CODEX_HOME/auth.json`` after a successful login. The
        exact schema is version-dependent; accept any of the known token
        locations (ChatGPT login nests under ``tokens``)."""
        try:
            with open(self._auth_path(user), "r") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        tokens = data.get("tokens") or {}
        return bool(
            (isinstance(tokens, dict) and tokens.get("access_token"))
            or data.get("access_token")
            or data.get("OPENAI_API_KEY"))

    # ------------------------------------------------------------------
    # Prompt assembly — Codex exec has no --append-system-prompt
    # ------------------------------------------------------------------
    def _build_prompt(self, session, body, paths, is_first, identity):
        """Fold the system prompt and the per-user identity block into the prompt
        text (Codex cannot take a system prompt flag).

        Ordered: system prompt, identity block, prior conversation (when not the
        first turn, replayed from the DB since Codex resume is by thread id but
        history is also surfaced for context), then the current user turn plus
        its attachment manifest. The system prompt is included on the first turn;
        the identity block (which reflects the current tool grant) every turn."""
        parts = []
        if is_first:
            parts.append(SYSTEM_PROMPT)
        parts.append(identity)
        if not is_first:
            history = session._history_for_prompt()
            if history:
                parts.append(
                    "--- PRIOR CONVERSATION ---\n" + history
                    + "\n--- END PRIOR CONVERSATION ---")
        parts.append("--- USER MESSAGE ---\n" + session._compose_prompt(body, paths))
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Launch: build `codex exec` argv/env (MCP injected via -c overrides)
    # ------------------------------------------------------------------
    def _prepare_run(self, ctx):
        last_msg_path = os.path.join(ctx["scratch"], "last_message.txt")
        argv = self._build_argv(ctx, last_msg_path)
        env = self._base_run_env(ctx)
        # The prompt is fed via STDIN (argv ends with "-"), never as an argv
        # token: it begins with the "--- ODOO USER CONTEXT" block, which codex's
        # parser would otherwise mistake for a CLI flag ("unexpected argument").
        return {
            "argv": argv,
            "env": env,
            "cwd": ctx["scratch"],
            "stdin": ctx["prompt"],
            "cleanup": [last_msg_path],
        }

    def _build_argv(self, ctx, last_msg_path):
        """Build the `codex exec` argv for this turn.

        Verified against codex-cli 0.139.0:
          first turn:  codex exec [OPTIONS] -
          later turns: codex exec [OPTIONS] resume <thread_id> -

        The exec OPTIONS (--json, -C, -o, -c, …) belong to `exec` and MUST come
        BEFORE the `resume` subcommand — `resume` itself only accepts `-c` plus
        the [SESSION_ID] [PROMPT] positionals, so passing e.g. `-C` after
        `resume` errors with "unexpected argument '-C'".

        Two flags work together for safe unattended operation:

        * --dangerously-bypass-approvals-and-sandbox: in `codex exec` (non-
          interactive) MCP tool calls are otherwise auto-cancelled — there is no
          TTY to approve them. This flag lets the Odoo MCP tools run.
        * --disable shell_tool: turns OFF Codex's built-in shell/command tool, so
          the agent has NO local execution at all. The only capabilities left are
          the loopback-only, token-gated Odoo MCP tools, which run under the
          acting user's own ACLs.

        --skip-git-repo-check is required because the scratch cwd is not a git
        repository.
        """
        resume = (not ctx["is_first"]) and ctx["cli_session_id"]
        argv = [ctx["cli_path"], "exec"]
        # exec-level options FIRST (apply to both fresh and resumed turns).
        argv += [
            "--json",
            "-C", ctx["scratch"],
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "--disable", "shell_tool",
            "--output-last-message", last_msg_path,
        ]
        # Only pin the model if explicitly configured; otherwise let Codex use
        # the account default (forcing an unsupported model 400s the turn).
        if ctx["model"]:
            argv += ["-m", ctx["model"]]
        # MCP server is injected per-invocation via -c overrides (race-free: the
        # per-session bridge token never touches a shared config file). The -c
        # value is parsed as TOML, so an inline table for `.env` is accepted.
        argv += self._mcp_overrides(ctx)
        # Then the resume subcommand + thread id, and finally "-" so codex reads
        # the PROMPT from stdin (see _prepare_run) instead of from argv.
        if resume:
            argv += ["resume", ctx["cli_session_id"]]
        argv.append("-")
        return argv

    def _mcp_overrides(self, ctx):
        bridge_env = self._bridge_env(ctx)
        return [
            "-c", "mcp_servers.odoo.command=%s" % _toml_str(ctx["python_bin"]),
            "-c", "mcp_servers.odoo.args=[%s]" % _toml_str(ctx["bridge_script"]),
            "-c", "mcp_servers.odoo.env=%s" % _toml_inline_table(bridge_env),
        ]

    def _post_run(self, ctx, rc, stderr):
        """Fallback assistant message: read the --output-last-message file.

        Codex has no explicit final-text event the way Claude's `result` does;
        when nothing was streamed the runner uses this file's contents."""
        path = os.path.join(ctx["scratch"], "last_message.txt")
        try:
            with open(path, "r") as f:
                return f.read().strip() or None
        except OSError:
            return None

    # ------------------------------------------------------------------
    # Stream parsing: codex stream-json (JSONL) -> common ops
    # ------------------------------------------------------------------
    def _normalize_event(self, evt, state):
        etype = evt.get("type") or ""
        ops = []

        # thread.started carries the codex thread/session id used to resume.
        if etype == "thread.started":
            tid = evt.get("thread_id") or (evt.get("thread") or {}).get("id")
            if tid:
                ops.append({"op": "session_id", "id": tid})
            return ops

        # item.* events carry the actual content (agent text, tool calls, …).
        if etype.startswith("item."):
            return self._normalize_item(evt.get("item") or {})

        if etype == "turn.completed":
            ops.append({"op": "done", "text": None})
            return ops

        if etype in ("turn.failed", "error"):
            # Codex emits either {"type":"error","message":"…"} (top-level) or a
            # nested {"error":{"message":"…"}} — accept both.
            err = evt.get("error")
            if isinstance(err, dict):
                text = err.get("message")
            else:
                text = err if isinstance(err, str) else None
            text = text or evt.get("message") or "Codex run failed."
            ops.append({"op": "error", "text": text})
            return ops

        # Unknown events: ignore (never raise).
        return ops

    def _normalize_item(self, item):
        ops = []
        # Codex labels the kind under "type" (within the item) or "item_type".
        itype = item.get("item_type") or item.get("type") or ""
        item_id = item.get("id") or item.get("item_id")

        if itype in ("agent_message", "assistant_message", "message"):
            text = (item.get("text") or item.get("message")
                    or self._content_text(item.get("content")) or "")
            if not text:
                return ops
            # Item events may arrive as started/updated/completed for one id;
            # Codex emits the latest FULL text, so replace rather than append.
            ops.append({"op": "text", "msg_id": item_id,
                        "text": text, "replace": True})
            return ops

        if itype in ("mcp_tool_call", "tool_call", "function_call",
                     "command_execution", "web_search"):
            name = (item.get("tool") or item.get("name")
                    or item.get("command") or itype)
            tinput = (item.get("arguments") or item.get("args")
                      or item.get("input") or {})
            if not isinstance(tinput, dict):
                tinput = {"arguments": tinput}
            ops.append({
                "op": "tool_use",
                "msg_id": item_id,
                "id": item_id or itype,
                "name": self._strip_tool_prefix(name),
                "input": tinput,
            })
            # Codex has no streamed tool_result event, so none is emitted here.
            return ops

        # reasoning / plan_update / file_change / others: not surfaced.
        return ops

    @staticmethod
    def _content_text(content):
        """Flatten a list-of-blocks content payload into plain text."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                b.get("text", "") for b in content if isinstance(b, dict))
        return ""

    # ------------------------------------------------------------------
    # Authentication — ChatGPT subscription device-code login
    # ------------------------------------------------------------------
    def _login_env(self, user):
        """Minimal env for spawning the codex CLI for THIS user."""
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }
        env.update(self._oauth_env(user))
        return env

    def _auth_start(self, user):
        """Begin a device-code login: spawn `codex login --device-auth`, capture
        the verification URL + one-time code, and leave the process running so
        Codex completes the exchange and writes auth.json.

        Returns ``{"url", "code", "raw", "authenticated"}`` for the chat gate."""
        if self._is_authenticated(user):
            return {"url": "", "code": "", "raw": "", "authenticated": True}

        self._reap_login(user)  # kill any previous in-flight attempt
        cli = self._resolve_cli_path()
        try:
            proc = subprocess.Popen(
                [cli, "login", "--device-auth"],
                cwd=self._home_dir(user), env=self._login_env(user),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, bufsize=1)
        except OSError as e:
            raise UserError(_("Could not start codex login: %s") % e)

        self.env["ir.config_parameter"].sudo().set_param(
            _LOGIN_PID % user.id, str(proc.pid))

        raw = self._capture_login(proc, _LOGIN_CAPTURE_S)
        raw = _ANSI_RE.sub("", raw)
        url_m = _URL_RE.search(raw)
        url = url_m.group(0) if url_m else ""
        m = _CODE_RE.search(raw)
        code = m.group(1) if m else ""
        if proc.poll() not in (None, 0) and not url:
            raise UserError(_(
                "codex login failed to start the device flow. Make sure device-"
                "code login is enabled for your ChatGPT account/workspace.\n%s"
            ) % (raw[:500] or "(no output)"))
        return {"url": url, "code": code, "raw": raw[:2000],
                "authenticated": self._is_authenticated(user)}

    @staticmethod
    def _capture_login(proc, wait_s):
        """Read the login process's stdout for up to ``wait_s`` seconds (without
        blocking on the long-lived process), returning the captured text."""
        q = queue.Queue()

        def _pump(pipe, out_q):
            try:
                for ln in pipe:
                    out_q.put(ln)
            finally:
                out_q.put(None)

        threading.Thread(target=_pump, args=(proc.stdout, q), daemon=True,
                         name="aidoo_codex_login_read").start()
        deadline = time.time() + wait_s
        buf = []
        while time.time() < deadline:
            try:
                ln = q.get(timeout=min(1.0, max(0.05, deadline - time.time())))
            except queue.Empty:
                if proc.poll() is not None:
                    break
                continue
            if ln is None:
                break
            buf.append(ln)
            # Strip ANSI before matching: the CLI wraps the code in color escapes
            # (e.g. "\x1b[94mIL70-LNADU\x1b[0m") whose trailing "m" would otherwise
            # defeat the \b boundary and prevent the early exit, stalling the run.
            text = _ANSI_RE.sub("", "".join(buf))
            # Stop early once we have both a device URL and a code.
            if _URL_RE.search(text) and _CODE_RE.search(text):
                break
        return "".join(buf)

    def _auth_status(self, user):
        """Are this user's credentials present? Reap the (finished) login
        process once authenticated."""
        ok = self._is_authenticated(user)
        if ok:
            self._reap_login(user)  # login done; drop the (now-exited) process
        return {"authenticated": ok, "auth_mode": self._auth_ui_mode}

    def _reap_login(self, user):
        """Kill any in-flight login subprocess for this user and clear the pid."""
        ICP = self.env["ir.config_parameter"].sudo()
        raw = ICP.get_param(_LOGIN_PID % user.id)
        if raw and raw.isdigit():
            try:
                os.kill(int(raw), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        ICP.set_param(_LOGIN_PID % user.id, "")

    def _auth_logout(self, user):
        """Remove this user's stored credentials (forces re-authentication)."""
        self._reap_login(user)
        cli = None
        try:
            cli = self._resolve_cli_path()
        except UserError:
            cli = None
        if cli:
            try:
                subprocess.run(
                    [cli, "logout"], cwd=self._home_dir(user),
                    env=self._login_env(user), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=30, check=False)
            except (OSError, subprocess.SubprocessError):
                pass
        try:
            os.remove(self._auth_path(user))
        except OSError:
            pass
        return {"authenticated": False}
