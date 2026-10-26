# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Claude (Anthropic Claude Code CLI) provider adapter.

Reference adapter — the other three (codex/gemini/grok) mirror this shape.
Drives the Claude Code CLI headless with stream-json output, a stdio MCP bridge
(``mcp.json``), and a ``settings.json`` that denies every built-in tool except a
sandboxed Read. Per-user auth is the Claude subscription OAuth (PKCE) flow: the
user authorizes in their browser and pastes the ``code#state`` back.
"""
import base64
import glob
import hashlib
import json
import logging
import os
import re
import shlex
import secrets
import shutil
import time

import requests

from odoo import _
from odoo.exceptions import UserError

from .base import AidooProvider, SYSTEM_PROMPT
from ..aidoo_session import (
    DISALLOWED_BUILTINS, UPLOADS_SUBDIR, WEB_TOOLS, WEB_TOOL_BUILTINS)

_logger = logging.getLogger(__name__)

# Public Claude Code OAuth client parameters (manual "copy code" redirect).
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
OAUTH_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
OAUTH_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
OAUTH_SCOPES = "org:create_api_key user:profile user:inference"
_PKCE_PARAM = "aidoo.claude.oauth_pkce.%s"

DEFAULT_CLI_GLOB = (
    "/opt/odoo/.local/share/code-server/extensions/"
    "anthropic.claude-code-*/resources/native-binary/claude")


class AidooProviderClaude(AidooProvider):
    _name = "aidoo.provider.claude"
    _inherit = "aidoo.provider"
    _description = "Aidoo Provider — Claude"

    _provider_code = "claude"
    _provider_label = "Claude"
    _accent_color = "#714b67"
    _provider_icon = "/aidoo/static/src/svg/claude-color.svg"
    _config_dir_name = ".claude"
    _default_cli_glob = DEFAULT_CLI_GLOB
    _default_model = "claude-sonnet-4-5"
    _auth_ui_mode = "oauth_code"
    _uses_system_prompt_flag = True
    # Claude is the only provider whose conversation id we mint client-side and
    # pass as --session-id on the first turn (then --resume on later turns).
    _mints_session_id = True

    # ------------------------------------------------------------------
    # CLI resolution (version-sorted: newest claude-code-X.Y.Z wins)
    # ------------------------------------------------------------------
    def _resolve_cli_path(self):
        explicit = self._pconfig("cli_path")
        if explicit and os.path.exists(explicit):
            return explicit
        pattern = self._pconfig("cli_glob", self._default_cli_glob)
        matches = glob.glob(pattern)
        if matches:
            def _ver_key(p):
                m = re.search(r"claude-code-(\d+)\.(\d+)\.(\d+)", p)
                return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)

            return sorted(matches, key=_ver_key)[-1]
        # Fall back to PATH lookup, same as the base class, for standalone
        # installs (e.g. CICDoo's /opt/odoo/.local/bin/claude) that aren't
        # bundled as a code-server extension.
        which = shutil.which(os.path.basename(pattern))
        if which:
            return which
        raise UserError(_(
            "Claude CLI binary not found. Set the System Parameter "
            "'aidoo.claude.cli_path' or 'aidoo.claude.cli_glob'. Tried: %s") % pattern)

    # ------------------------------------------------------------------
    # Runtime env: HOME + CLAUDE_CONFIG_DIR (per user)
    # ------------------------------------------------------------------
    def _oauth_env(self, user):
        return {
            "HOME": self._home_dir(user),
            "CLAUDE_CONFIG_DIR": self._config_dir(user, create=True),
        }

    def _session_exists(self, session, config_dir):
        """Claude can only --resume a session whose transcript exists under the
        current CLAUDE_CONFIG_DIR."""
        sid = session.cli_session_id
        if not sid:
            return False
        pattern = os.path.join(config_dir, "projects", "*", sid + ".jsonl")
        return bool(glob.glob(pattern))

    def _sync_cli_account(self, user):
        """Overwrite display-only fields in .claude.json so the CLI's injected
        '# userEmail' / account name reflects the Odoo user, not the personal
        Claude account."""
        path = os.path.join(self._config_dir(user, create=True), ".claude.json")
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return False
        acct = data.get("oauthAccount")
        if not isinstance(acct, dict):
            return False
        email = user._ai_account_email()
        changed = False
        if email and acct.get("emailAddress") != email:
            acct["emailAddress"] = email
            changed = True
        if user.name and acct.get("displayName") != user.name:
            acct["displayName"] = user.name
            changed = True
        if not changed:
            return False
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
        return True

    # ------------------------------------------------------------------
    # Launch: write mcp.json + settings.json, build argv/env
    # ------------------------------------------------------------------
    def _prepare_run(self, ctx):
        mcp_config_path = self._write_mcp_config(ctx)
        settings_path = self._write_settings(ctx)
        sysprompt_path = self._write_system_prompt(ctx)
        argv = self._build_argv(ctx, mcp_config_path, settings_path, sysprompt_path)
        env = self._base_run_env(ctx)
        return {
            "argv": argv,
            "env": env,
            "cwd": ctx["scratch"],
            "stdin": None,
            "cleanup": [mcp_config_path, settings_path, sysprompt_path],
        }

    def _write_system_prompt(self, ctx):
        """Persona + skills can exceed the kernel's 128KiB per-argv-string limit
        (execve fails with E2BIG "Argument list too long"), so the system prompt
        goes through a file, never argv."""
        path = os.path.join(ctx["scratch"], "system_prompt.txt")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(SYSTEM_PROMPT + "\n\n" + ctx["identity"])
        return path

    def _build_argv(self, ctx, mcp_config_path, settings_path, sysprompt_path):
        odoo_tools = [t for t in ctx["allowed_tools"] if t not in WEB_TOOLS]
        web_builtins = [WEB_TOOL_BUILTINS[t] for t in ctx["allowed_tools"] if t in WEB_TOOLS]
        allowed = ",".join(
            ["mcp__odoo__%s" % t for t in odoo_tools] + web_builtins + ["ToolSearch"])
        argv = [
            ctx["cli_path"], "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--model", ctx["model"],
            "--max-turns", str(ctx["max_turns"]),
            "--permission-mode", "default",
            "--strict-mcp-config",
            "--mcp-config", mcp_config_path,
            "--settings", settings_path,
            "--allowedTools", allowed,
            "--append-system-prompt-file", sysprompt_path,
        ]
        # Resume only when this conversation's transcript exists under the
        # current CLAUDE_CONFIG_DIR; otherwise start fresh (reusing the UUID) so
        # the turn doesn't fail with "No conversation found".
        is_first = ctx["is_first"]
        if not is_first:
            config_dir = ctx["oauth_env"].get("CLAUDE_CONFIG_DIR", "")
            pattern = os.path.join(
                config_dir, "projects", "*", ctx["cli_session_id"] + ".jsonl")
            if not glob.glob(pattern):
                is_first = True
        if is_first:
            argv += ["--session-id", ctx["cli_session_id"]]
        else:
            argv += ["--resume", ctx["cli_session_id"]]
        argv.append(ctx["prompt"])
        return argv

    def _write_settings(self, ctx):
        """Deny every built-in tool except a Read sandboxed to the uploads dir
        (path-scoped allow rule + a PreToolUse read_guard hook)."""
        web_builtins = [WEB_TOOL_BUILTINS[t] for t in ctx["allowed_tools"] if t in WEB_TOOLS]
        keep = {"ListMcpResources", "ReadMcpResource", "ToolSearch", "Read"} | set(web_builtins)
        deny = [t for t in DISALLOWED_BUILTINS if t not in keep]
        uploads_dir = os.path.join(ctx["scratch"].rstrip("/"), UPLOADS_SUBDIR)
        uploads_glob = "Read(/%s/**)" % uploads_dir
        guard = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "bridge", "read_guard.py")
        guard_cmd = "%s %s %s" % (
            shlex.quote(ctx["python_bin"]), shlex.quote(guard), shlex.quote(uploads_dir))
        cfg = {
            "permissions": {"deny": deny, "allow": [uploads_glob]},
            "hooks": {"PreToolUse": [{
                "matcher": "Read",
                "hooks": [{"type": "command", "command": guard_cmd}],
            }]},
        }
        path = os.path.join(ctx["scratch"], "settings.json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f)
        return path

    def _write_mcp_config(self, ctx):
        cfg = {"mcpServers": {"odoo": {
            "type": "stdio",
            "command": ctx["python_bin"],
            "args": [ctx["bridge_script"]],
            "env": self._bridge_env(ctx),
        }}}
        path = os.path.join(ctx["scratch"], "mcp.json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f)
        return path

    # ------------------------------------------------------------------
    # Stream parsing: Claude stream-json -> common ops
    # ------------------------------------------------------------------
    def _normalize_event(self, evt, state):
        etype = evt.get("type")
        ops = []
        if etype == "system" and evt.get("subtype") == "init":
            sid = evt.get("session_id")
            if sid:
                ops.append({"op": "session_id", "id": sid})
            return ops
        if etype == "assistant":
            message = evt.get("message") or {}
            cid = message.get("id")
            for block in message.get("content") or []:
                btype = block.get("type")
                if btype == "text":
                    ops.append({"op": "text", "msg_id": cid,
                                "text": block.get("text") or ""})
                elif btype == "tool_use":
                    ops.append({
                        "op": "tool_use", "msg_id": cid,
                        "id": block.get("id"),
                        "name": self._strip_tool_prefix(block.get("name")),
                        "input": block.get("input") or {},
                    })
            return ops
        if etype == "user":
            message = evt.get("message") or {}
            for block in message.get("content") or []:
                if block.get("type") == "tool_result":
                    rc = block.get("content")
                    if isinstance(rc, list):
                        rc = "\n".join(
                            b.get("text", "") for b in rc if isinstance(b, dict))
                    ops.append({
                        "op": "tool_result", "id": block.get("tool_use_id"),
                        "text": rc or "", "is_error": bool(block.get("is_error")),
                    })
            return ops
        if etype == "result":
            text = evt.get("result") or ""
            if not text and evt.get("errors"):
                text = "; ".join(str(e) for e in evt["errors"])
            if evt.get("is_error"):
                ops.append({"op": "error", "text": text})
            else:
                ops.append({"op": "done", "text": text or None})
            return ops
        return ops

    # ------------------------------------------------------------------
    # Authentication — Claude subscription OAuth (PKCE, paste-back code)
    # ------------------------------------------------------------------
    def _credentials_path(self, user):
        return os.path.join(self._config_dir(user), ".credentials.json")

    def _is_authenticated(self, user):
        try:
            with open(self._credentials_path(user)) as f:
                data = json.load(f)
            return bool((data.get("claudeAiOauth") or {}).get("accessToken"))
        except (OSError, ValueError):
            return False

    def _auth_start(self, user):
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(32)
        self.env["ir.config_parameter"].sudo().set_str(
            _PKCE_PARAM % user.id, json.dumps({"verifier": verifier, "state": state}))
        from urllib.parse import urlencode
        params = {
            "code": "true", "client_id": OAUTH_CLIENT_ID, "response_type": "code",
            "redirect_uri": OAUTH_REDIRECT_URI, "scope": OAUTH_SCOPES,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
        }
        return {"url": "%s?%s" % (OAUTH_AUTHORIZE_URL, urlencode(params))}

    def _auth_complete(self, user, pasted_code):
        pasted_code = (pasted_code or "").strip()
        if not pasted_code:
            raise UserError(_("Paste the authorization code from Claude."))
        ICP = self.env["ir.config_parameter"].sudo()
        raw = ICP.get_str(_PKCE_PARAM % user.id)
        if not raw:
            raise UserError(_("No login in progress. Click “Login with Claude” first."))
        pkce = json.loads(raw)
        code, _sep, state = pasted_code.partition("#")
        if state and pkce.get("state") and state != pkce["state"]:
            raise UserError(_("Authorization state mismatch. Please log in again."))
        payload = {
            "grant_type": "authorization_code", "code": code,
            "state": state or pkce.get("state"), "client_id": OAUTH_CLIENT_ID,
            "redirect_uri": OAUTH_REDIRECT_URI, "code_verifier": pkce["verifier"],
        }
        try:
            resp = requests.post(
                OAUTH_TOKEN_URL, json=payload,
                headers={"Content-Type": "application/json"}, timeout=30)
        except requests.RequestException as e:
            raise UserError(_("Could not reach Claude to exchange the code: %s") % e)
        if resp.status_code != 200:
            _logger.warning("Claude OAuth exchange failed (%s): %s",
                            resp.status_code, resp.text[:500])
            raise UserError(_(
                "Claude rejected the authorization code (HTTP %s). The code may "
                "have expired — please log in again.") % resp.status_code)
        data = resp.json()
        access = data.get("access_token")
        if not access:
            raise UserError(_("Claude did not return an access token."))
        expires_in = int(data.get("expires_in") or 0)
        scopes = (data.get("scope") or OAUTH_SCOPES).split()
        creds = {"claudeAiOauth": {
            "accessToken": access,
            "refreshToken": data.get("refresh_token") or "",
            "expiresAt": int((time.time() + expires_in) * 1000),
            "scopes": scopes,
            "subscriptionType": data.get("subscription_type") or "",
        }}
        self._write_user_file(user, self._credentials_path(user), json.dumps(creds))
        ICP.set_str(_PKCE_PARAM % user.id, "")
        return {"authenticated": True}

    def _auth_logout(self, user):
        try:
            os.remove(self._credentials_path(user))
        except OSError:
            pass
        return {"authenticated": False}
