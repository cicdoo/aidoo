# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Antigravity (Google Antigravity ``agy`` CLI) provider adapter — EXPERIMENTAL.

Drives the headless ``agy`` CLI (``agy -p``). Unlike the other providers, agy is a
TUI coding agent with **no structured/streaming output**: print mode emits only the
final answer as plain text. So this adapter is *text-only* — no token streaming, no
tool-call chips, no artifact panel. The single final message is captured by
redirecting agy's stdout to a file (the shared runner reads JSONL; agy emits none,
so we recover the text in ``_post_run``).

Safety: agy ships built-in shell/file-write/web tools. Its permission engine only
recognises the actions ``command``, ``write_file`` and ``mcp`` (verified empirically
— every other action name is rejected as "unknown action"). We deny ``command(*)``
(shell) and ``write_file(*)`` (file write) via
``<home>/.gemini/antigravity-cli/settings.json``, leaving only the gated Odoo MCP
tools plus read-only file tools (confined to the workspace, for attachments).

KNOWN LIMITATION: agy's **web tools (read_url_content / search_web / browser) cannot
be denied** — there is no permission action for them — so this provider has ungated
internet access (a data-exfiltration vector under an untrusted-LLM threat model). It
is therefore gated as experimental/opt-in (``aidoo.antigravity.enabled``) and
documented in SECURITY.md. Do not enable it for zero-trust deployments.

Per-user auth reuses the Gemini-style import flow: the user runs ``agy`` once on a
machine with a browser and pastes the resulting
``~/.gemini/antigravity-cli/antigravity-oauth-token`` (a portable OAuth token file).
"""
import glob
import json
import os

from odoo import _
from odoo.exceptions import UserError

from .base import AidooProvider, SYSTEM_PROMPT

# agy permission actions that actually exist (others are rejected as "unknown
# action"). We deny shell + file-write; ``mcp`` stays allowed for the Odoo bridge.
SAFETY_DENY = ["command(*)", "write_file(*)"]


class AidooProviderAntigravity(AidooProvider):
    _name = "aidoo.provider.antigravity"
    _inherit = "aidoo.provider"
    _description = "Aidoo Provider — Antigravity"

    _provider_code = "antigravity"
    _provider_label = "Antigravity"
    _accent_color = "#4285F4"
    _provider_icon = "fa-rocket"
    _config_dir_name = ".gemini"
    _default_cli_glob = "/usr/local/bin/agy"
    _default_model = ""
    _auth_ui_mode = "import_or_key"
    _uses_system_prompt_flag = False
    # Auth panel: no API key for the consumer flow; guide the user to the token file.
    _auth_key_supported = False
    _auth_import_placeholder = (
        "Paste the full contents of antigravity-oauth-token here "
        '(starts with {"token":{"access_token":...}})')
    _auth_import_help = (
        "1. On a computer with a web browser, install the Antigravity CLI and run "
        "it once:\n"
        "      curl -fsSL https://antigravity.google/cli/install.sh | bash\n"
        "      agy        (sign in with your Google account)\n"
        "2. Open the token file the CLI created:\n"
        "      ~/.gemini/antigravity-cli/antigravity-oauth-token\n"
        "      (on the machine where you ran agy)\n"
        "3. Copy its entire contents and paste them below, then Import & connect.")
    # Opt-in: hidden from the provider picker unless aidoo.antigravity.enabled is
    # set, because web tools cannot be sandboxed (see module docstring + SECURITY).
    _experimental = True

    # ------------------------------------------------------------------
    # Per-user paths (agy stores everything under <home>/.gemini/antigravity-cli)
    # ------------------------------------------------------------------
    def _agy_dir(self, user, create=False):
        path = os.path.join(self._home_dir(user), ".gemini", "antigravity-cli")
        if create:
            os.makedirs(os.path.join(path, "cache"), mode=0o700, exist_ok=True)
        return path

    def _token_path(self, user):
        return os.path.join(self._agy_dir(user), "antigravity-oauth-token")

    def _settings_path(self, user):
        return os.path.join(self._agy_dir(user), "settings.json")

    def _write_safety_settings(self, user):
        """Write the per-user settings.json that denies shell + file-write.

        Idempotent; called on auth and before every run so the deny rules are
        always present even if the file was tampered with."""
        self._agy_dir(user, create=True)
        cfg = {"enableTelemetry": False,
               "permissions": {"deny": list(SAFETY_DENY)}}
        self._write_user_file(user, self._settings_path(user), json.dumps(cfg))
        # Mark onboarding complete so headless runs never wait on an onboarding
        # prompt (token-only auth is sufficient, but this is cheap insurance).
        onb = os.path.join(self._agy_dir(user), "cache", "onboarding.json")
        if not os.path.exists(onb):
            self._write_user_file(user, onb, json.dumps(
                {"consumerOnboardingComplete": True, "onboardingComplete": True}))

    # ------------------------------------------------------------------
    # Runtime env — agy reads auth + config from $HOME/.gemini
    # ------------------------------------------------------------------
    def _oauth_env(self, user):
        self._write_safety_settings(user)
        return {"HOME": self._home_dir(user)}

    # ------------------------------------------------------------------
    # Prompt assembly — no system-prompt flag; fold everything in (like Gemini)
    # ------------------------------------------------------------------
    # agy aborts the whole turn on the first MCP tool error (it does not feed the
    # error back for self-correction like the other CLIs). Nudge the model to make
    # complete, well-formed calls so an exploratory mistake doesn't kill the turn.
    AGY_TOOL_HINT = (
        "TOOL DISCIPLINE: make each tool call complete and correct on the first "
        "try — always pass required arguments (e.g. a concrete `model` like "
        "'res.partner', never null). A failed tool call aborts the whole turn, so "
        "do not call a tool speculatively with missing arguments."
    )

    def _build_prompt(self, session, body, paths, is_first, identity):
        parts = [SYSTEM_PROMPT, self.AGY_TOOL_HINT, identity]
        if not is_first:
            history = session._history_for_prompt()
            if history:
                parts.append("--- CONVERSATION SO FAR ---\n" + history)
        parts.append("--- USER MESSAGE ---\n" + session._compose_prompt(body, paths))
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Launch: write workspace MCP config; capture plain text via a wrapper
    # ------------------------------------------------------------------
    def _out_path(self, scratch):
        return os.path.join(scratch, ".aidoo_agy_out.txt")

    def _prepare_run(self, ctx):
        mcp_path = self._write_mcp_config(ctx)
        out_file = self._out_path(ctx["scratch"])
        err_file = out_file + ".err"

        # Redirect agy's stdout/stderr to files via a wrapper. Args are passed as
        # positional parameters ($1..$N), never string-interpolated, so neither the
        # prompt nor any path can inject shell. agy reads the prompt from -p.
        inner = ('exec "$1" -p "$2" --add-dir "$3" --print-timeout "$4"'
                 ' >"$5" 2>"$6"')
        extra = []
        if ctx["model"]:
            inner = inner.replace('--print-timeout "$4"',
                                  '--print-timeout "$4" --model "$7"')
            extra = [ctx["model"]]
        argv = ["bash", "-c", inner, "aidoo-antigravity",
                ctx["cli_path"], ctx["prompt"], ctx["uploads_dir"],
                "%ss" % ctx["timeout_s"], out_file, err_file] + extra
        # NOTE: deliberately NO --dangerously-skip-permissions. In print mode the
        # allowed MCP tools auto-proceed while the deny rules still block shell +
        # file-write.
        return {
            "argv": argv,
            "env": self._base_run_env(ctx),
            "cwd": ctx["scratch"],
            "stdin": None,                       # prompt is an argv value
            "cleanup": [mcp_path, out_file, err_file],
        }

    def _write_mcp_config(self, ctx):
        """Per-run workspace MCP config: <scratch>/.agents/mcp_config.json.

        Workspace-scoped (not the global ~/.gemini/config) so a user's concurrent
        sessions never race on the bridge token. Verified agy loads it when run
        from the workspace cwd."""
        cfg = {"mcpServers": {"odoo": {
            "command": ctx["python_bin"],
            "args": [ctx["bridge_script"]],
            "env": self._bridge_env(ctx),
        }}}
        adir = os.path.join(ctx["scratch"], ".agents")
        os.makedirs(adir, mode=0o700, exist_ok=True)
        path = os.path.join(adir, "mcp_config.json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f)
        return path

    def _normalize_event(self, evt, state):
        # agy emits no JSON event stream; the final text is recovered in _post_run.
        return []

    def _post_run(self, ctx, rc, stderr):
        """Recover the plain-text answer agy wrote to the redirect file. If it is
        empty, surface the last error from agy's own log (agy exits 0 even on hard
        errors, writing them only to its log file)."""
        out = self._read_text(self._out_path(ctx["scratch"]))
        if out and out.strip():
            return out.strip()
        return self._last_log_error(ctx) or None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _read_text(path):
        try:
            with open(path, "r") as f:
                return f.read()
        except OSError:
            return ""

    def _last_log_error(self, ctx):
        home = ctx["oauth_env"].get("HOME")
        if not home:
            return None
        logs = sorted(glob.glob(os.path.join(
            home, ".gemini", "antigravity-cli", "log", "cli-*.log")))
        if not logs:
            return None
        try:
            with open(logs[-1], "r", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            return None
        for ln in reversed(lines):
            # Glog error lines start with 'E'; keep the message after the location.
            if ln.startswith("E"):
                msg = ln.split("]", 1)[-1].strip()
                return _("Antigravity error: %s") % (msg[:400] or ln.strip()[:400])
        return None

    # ------------------------------------------------------------------
    # Authentication — paste the agy oauth-token file (Gemini-style import)
    # ------------------------------------------------------------------
    def _has_token(self, user):
        try:
            with open(self._token_path(user), "r") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return False
        tok = data.get("token") if isinstance(data, dict) else None
        tok = tok if isinstance(tok, dict) else (data if isinstance(data, dict) else {})
        return bool(tok.get("access_token") or tok.get("refresh_token"))

    def _is_authenticated(self, user):
        return self._has_token(user)

    def _auth_import(self, user, blob):
        """Validate and store the pasted antigravity-oauth-token JSON."""
        blob = (blob or "").strip()
        if not blob:
            raise UserError(_("Paste your antigravity-oauth-token file content."))
        try:
            data = json.loads(blob)
        except ValueError:
            raise UserError(_(
                "That is not valid JSON. Run `agy` and sign in on a machine with a "
                "browser, then paste the content of "
                "~/.gemini/antigravity-cli/antigravity-oauth-token."))
        tok = data.get("token") if isinstance(data, dict) else None
        tok = tok if isinstance(tok, dict) else data
        if not isinstance(tok, dict) or not (
                tok.get("access_token") or tok.get("refresh_token")):
            raise UserError(_(
                "The pasted token has no access_token/refresh_token. Paste the full "
                "content of ~/.gemini/antigravity-cli/antigravity-oauth-token."))
        # Create the nested antigravity-cli dir (and write the deny settings)
        # before the token, since the token lives below the base .gemini dir.
        self._write_safety_settings(user)
        self._write_user_file(user, self._token_path(user), json.dumps(data))
        return {"authenticated": True}

    def _auth_set_key(self, user, key):
        # API-key / Vertex-project auth is not wired up yet (consumer token only).
        raise UserError(_(
            "Antigravity connects by importing your antigravity-oauth-token; there "
            "is no API-key option."))

    def _auth_logout(self, user):
        try:
            os.remove(self._token_path(user))
        except OSError:
            pass
        return {"authenticated": False}
