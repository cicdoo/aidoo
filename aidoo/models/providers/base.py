# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Provider adapter base.

Aidoo merges four per-AI-provider assistants (Claude / Codex / Gemini / Grok)
into one module. Everything generic — session lifecycle, the MCP bridge token,
file uploads, tool gating, the OWL chat client — lives in the shared models.
Everything that differs *per AI CLI* lives in a provider adapter: an
``AbstractModel`` whose ``_name`` is ``aidoo.provider.<code>``.

The session stores the chosen ``provider`` and the generic runner resolves the
adapter with ``env["aidoo.provider." + session.provider]``. Adapters override
only the five axes that actually diverge:

1. CLI invocation  -> ``_prepare_run`` (argv / env / cwd / stdin / cleanup)
2. Run config files -> written inside ``_prepare_run``
3. Stream parsing  -> ``_normalize_event`` (emits a common op stream)
4. Authentication  -> the ``_auth_*`` / ``_is_authenticated`` / ``_oauth_env`` group
5. Identity/prompt assembly + resume strategy -> ``_build_prompt`` / ``_session_exists``

This base implements the Claude-shaped defaults (per-user OAuth, JSON MCP config,
identity appended to the prompt) so each adapter overrides as little as possible.
"""
import json
import logging
import os

from odoo import models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Provider-neutral system prompt. Claude passes it via --append-system-prompt;
# the other CLIs fold it into the prompt text (see each adapter's _build_prompt).
SYSTEM_PROMPT = (
    "You are an AI assistant embedded inside an Odoo 19 ERP system. "
    "You help the current Odoo user query data and build reports. "
    "You act through the provided `mcp__odoo__*` tools — you have no shell access. "
    "Web access (WebFetch/WebSearch / web search) is available only when the tool "
    "grant in the ODOO USER CONTEXT block below lists it; otherwise assume you "
    "cannot reach the web. The only filesystem tool available is a file Read, and "
    "it is restricted to the files the user attached to this conversation (in your "
    "working directory); use it to open those attachments when the user refers to "
    "them, but do not attempt to read or write anything else. All actions run with "
    "the user's own Odoo permissions, so respect AccessError results and never try "
    "to work around them. "
    "Use `model_introspect` to discover models/fields before querying — consult its "
    "`effective_access` map to see which actions (read/write/create/unlink) are actually "
    "available to you here before attempting them. "
    "Use `orm_search_read`/`orm_read`/`orm_call` for normal data access (these honor "
    "record rules). Use `sql_select` only for read-only reporting when available. "
    "Write tools (`orm_create`/`orm_write`/`orm_unlink`) may be available; only some "
    "users are granted them. When you change data, confirm what you did concisely. "
    "Action tools may also be available (check `model_introspect`'s `ai_tools_allowed`): "
    "use `orm_action` to call business methods like `action_confirm`/`action_post`/"
    "`button_validate` on records (method names are allowlisted), `run_wizard` to create "
    "and run a wizard in one step, and `run_server_action` to run an admin-allowlisted "
    "server action. Prefer these over raw `orm_write` when an Odoo business action exists, "
    "so state machines and validations run correctly. "
    "Be concise. When you present data, format it as a clear Markdown table when useful. "
    "IDENTITY: You are acting on behalf of a specific Odoo user, whose identity, company, "
    "locale and roles are given in the 'ODOO USER CONTEXT' block below. You are THAT Odoo "
    "user's assistant — never identify yourself as the underlying AI subscription account, "
    "and address the user by their Odoo name. Any account email shown elsewhere in your "
    "environment (e.g. a 'userEmail' context line) is the underlying AI subscription used "
    "to run you — it is NOT the user; the authoritative email and identity are the ones in "
    "the ODOO USER CONTEXT block. Assume ONLY the roles and privileges listed in "
    "that block; never assume administrator rights you have not been shown. Every tool runs "
    "with this user's own Odoo permissions, so an AccessError is authoritative: report it "
    "plainly and never try to work around it. "
    "CRITICAL: Only ever call tools through the real tool-calling mechanism. NEVER write "
    "tool calls, XML/function tags, or fabricated tool results as text in your reply, and "
    "NEVER invent data. If the mcp__odoo__* tools are not visible to you, reply exactly "
    "'AI tools are not available right now, please retry.' and nothing else — do not guess."
)


class AidooProvider(models.AbstractModel):
    _name = "aidoo.provider"
    _description = "Aidoo AI Provider Adapter (base)"

    # ------------------------------------------------------------------
    # Per-provider identity (overridden by subclasses)
    # ------------------------------------------------------------------
    _provider_code = ""           # e.g. "claude"
    _provider_label = ""          # e.g. "Claude"
    _accent_color = "#714b67"     # chart/report accent in the UI
    _config_dir_name = ".aidoo"   # per-user CLI config dir under HOME
    _default_cli_glob = ""        # where to find the CLI binary
    _default_model = ""           # default --model (empty = account default)
    # Auth UI the OWL client should render: one of
    #   "oauth_code"     — open URL, paste back code   (Claude)
    #   "device_poll"    — show device URL+code, poll  (Codex)
    #   "import_or_key"  — paste creds blob OR API key (Gemini / Grok)
    _auth_ui_mode = "oauth_code"
    # True when the CLI takes the system prompt via a dedicated flag rather than
    # folded into the user prompt text (Claude: --append-system-prompt).
    _uses_system_prompt_flag = True

    # ------------------------------------------------------------------
    # Descriptor consumed by the frontend (provider picker + auth panel)
    # ------------------------------------------------------------------
    def _descriptor(self):
        self.ensure_one() if self else None
        return {
            "code": self._provider_code,
            "label": self._provider_label,
            "accent": self._accent_color,
            "auth_mode": self._auth_ui_mode,
        }

    # ------------------------------------------------------------------
    # Config helpers (aidoo.<code>.<key> with fallback to aidoo.<key>)
    # ------------------------------------------------------------------
    def _pconfig(self, key, default=None):
        return self.env["aidoo.session"]._pconfig(self._provider_code, key, default)

    def _model(self):
        return self._pconfig("model", self._default_model) or self._default_model

    # ------------------------------------------------------------------
    # Per-user home / config dir
    # ------------------------------------------------------------------
    def _home_root(self):
        return self._pconfig(
            "home_root", "/var/lib/odoo/aidoo_%s_home" % self._provider_code)

    def _home_dir(self, user):
        return os.path.join(self._home_root(), str(user.id))

    def _config_dir(self, user, create=False):
        """The per-user CLI config dir (holds this provider's credentials)."""
        path = os.path.join(self._home_dir(user), self._config_dir_name)
        if create:
            try:
                os.makedirs(path, mode=0o700, exist_ok=True)
            except OSError as e:
                raise UserError(_("Cannot create %(label)s config dir %(path)s: %(err)s",
                                  label=self._provider_label, path=path, err=e))
        return path

    def _write_user_file(self, user, path, content):
        """Atomically write a per-user credential file (mode 0600)."""
        self._config_dir(user, create=True)
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
        return True

    # ------------------------------------------------------------------
    # CLI resolution
    # ------------------------------------------------------------------
    def _resolve_cli_path(self):
        """Explicit config path, else first glob match, else error.

        Claude overrides this to version-sort multiple matches."""
        import glob
        explicit = self._pconfig("cli_path")
        if explicit and os.path.exists(explicit):
            return explicit
        pattern = self._pconfig("cli_glob", self._default_cli_glob)
        matches = glob.glob(pattern)
        if matches:
            return sorted(matches)[-1]
        # Fall back to PATH lookup by the bare binary name.
        import shutil
        which = shutil.which(os.path.basename(pattern))
        if which:
            return which
        raise UserError(_(
            "%(label)s CLI binary not found. Set the System Parameter "
            "'aidoo.%(code)s.cli_path' or 'aidoo.%(code)s.cli_glob'. Tried: %(pat)s",
            label=self._provider_label, code=self._provider_code, pat=pattern))

    # ------------------------------------------------------------------
    # Runtime environment for a CLI run (HOME + provider auth env)
    # ------------------------------------------------------------------
    def _oauth_env(self, user):
        """Env pointing the CLI at THIS user's private config dir.

        Base implementation (Claude-shaped): HOME + the per-user config dir.
        Subclasses add provider-specific vars (CODEX_HOME, GEMINI_API_KEY, …)."""
        return {"HOME": self._home_dir(user)}

    # ------------------------------------------------------------------
    # Identity / resume helpers
    # ------------------------------------------------------------------
    def _sync_cli_account(self, user):
        """Align the CLI's stored account identity with the Odoo user.

        Only Claude needs this (patches .claude.json); others no-op."""
        return False

    def _session_exists(self, session, config_dir):
        """True if a resumable transcript for this conversation exists.

        Only Claude can check this (transcript .jsonl on disk). Providers that
        replay history into the prompt return False so the runner starts fresh
        and relies on the adapter's prompt history."""
        return False

    def _build_prompt(self, session, body, paths, is_first, identity):
        """Assemble the final prompt text handed to the CLI.

        Base (Claude-shaped): identity goes via the system-prompt flag, so the
        prompt is just the user body plus an attachment manifest. Providers that
        cannot pass a system prompt fold ``identity`` (and chat history) in here.
        """
        return session._compose_prompt(body, paths)

    # ------------------------------------------------------------------
    # Bridge wiring shared by every provider
    # ------------------------------------------------------------------
    def _bridge_env(self, ctx):
        """Env dict for the stdio MCP bridge child (same for all providers)."""
        from ..aidoo_session import WEB_TOOLS
        return {
            "AI_ODOO_BASE": ctx["base_url"],
            "AI_ODOO_DB": ctx["target_db"],
            "AI_ODOO_SID": ctx["routing_sid"],
            "AI_BRIDGE_TOKEN": ctx["token"],
            "AI_SESSION_ID": str(ctx["session_id"]),
            "AI_ALLOWED_TOOLS": ",".join(
                t for t in ctx["allowed_tools"] if t not in WEB_TOOLS),
            "AI_EXCLUDED_MODELS": ",".join(ctx["excluded_models"]),
        }

    def _base_run_env(self, ctx):
        """PATH/LANG + the user's auth env — the floor every provider builds on."""
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }
        env.update(ctx["oauth_env"])
        return env

    # ------------------------------------------------------------------
    # THE launch hook — must be implemented per provider
    # ------------------------------------------------------------------
    def _prepare_run(self, ctx):
        """Return everything the generic runner needs to spawn the CLI:

            {
              "argv":    [list of str],
              "env":     {env dict},
              "cwd":     str,
              "stdin":   str | None,   # prompt fed via stdin, else None
              "cleanup": [paths to unlink after the run],
            }
        """
        raise NotImplementedError(
            "Provider %s must implement _prepare_run" % self._provider_code)

    def _post_run(self, ctx, rc, stderr):
        """Optional fallback text when the CLI streamed no assistant message
        (e.g. read it from a --output-last-message file). Return None if N/A."""
        return None

    # ------------------------------------------------------------------
    # THE stream parser — normalize one CLI event into common ops.
    # Op kinds the generic runner understands:
    #   {"op": "session_id", "id": str}
    #   {"op": "text",        "msg_id": str|None, "text": str}
    #   {"op": "tool_use",    "msg_id": str|None, "id": str, "name": str, "input": dict}
    #   {"op": "tool_result", "id": str, "text": str, "is_error": bool}
    #   {"op": "done",        "text": str|None}
    #   {"op": "error",       "text": str}
    # ``state`` is a per-run mutable dict the parser may use for bookkeeping.
    # ------------------------------------------------------------------
    def _normalize_event(self, evt, state):
        raise NotImplementedError(
            "Provider %s must implement _normalize_event" % self._provider_code)

    @staticmethod
    def _strip_tool_prefix(name):
        """Strip any odoo MCP namespace prefix from a tool name."""
        name = name or ""
        for pref in ("mcp__odoo__", "mcp_odoo_", "odoo__", "odoo.", "odoo_"):
            if name.startswith(pref):
                return name[len(pref):]
        return name

    # ------------------------------------------------------------------
    # Authentication contract. Each adapter implements the subset matching
    # its _auth_ui_mode; the unified controller dispatches here.
    # ------------------------------------------------------------------
    def _auth_status(self, user):
        return {"authenticated": self._is_authenticated(user),
                "auth_mode": self._auth_ui_mode}

    def _is_authenticated(self, user):
        raise NotImplementedError

    def _auth_start(self, user):
        raise UserError(_("This provider does not support that login step."))

    def _auth_complete(self, user, code):
        raise UserError(_("This provider does not support that login step."))

    def _auth_import(self, user, blob):
        raise UserError(_("This provider does not support credential import."))

    def _auth_set_key(self, user, key):
        raise UserError(_("This provider does not support API-key login."))

    def _auth_logout(self, user):
        raise NotImplementedError
