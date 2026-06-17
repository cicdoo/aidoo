# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Shared per-user AI state.

The provider-agnostic bits live here: the AI tool grant, zero-trust mode, the
account email, and the authoritative identity/role block injected into the
assistant's system prompt. Everything provider-specific (credential storage,
login flow, is-authenticated probe) is delegated to the provider adapter
(``aidoo.provider.<code>``)."""
import logging

from odoo import fields, models, _
from odoo.tools import str2bool

from .aidoo_session import (
    READ_TOOLS, ALL_TOOLS, READONLY_TOOL_SET, WEB_TOOLS, PROVIDERS)

_logger = logging.getLogger(__name__)


class ResUsers(models.Model):
    _inherit = "res.users"

    # --- Per-user AI tool permissions (manager-managed, provider-agnostic) ---
    aidoo_tool_ids = fields.Many2many(
        "aidoo.tool", "aidoo_user_tool_rel", "user_id", "tool_id",
        string="AI Tools Allowed",
        help="Tools this user may invoke through the AI Assistant. "
             "Leave empty to allow all read-only tools.")
    aidoo_zero_trust_mode = fields.Selection(
        [("inherit", "Inherit global default"),
         ("on", "Zero-trust (read-only)"),
         ("off", "Allow writes")],
        default="inherit", string="AI Zero-Trust Mode",
        help="When zero-trust is active, only read-only tools are available to "
             "this user — write tools are stripped regardless of the selection "
             "above. 'Inherit' follows the global default in Settings.")
    # Optional per-user Google Cloud project id (exported as GOOGLE_CLOUD_PROJECT
    # by the Gemini adapter). Needed by some account types.
    aidoo_cloud_project = fields.Char(
        string="Google Cloud Project",
        help="Optional Google Cloud project id, exported as GOOGLE_CLOUD_PROJECT "
             "when running the Gemini CLI for this user.")

    @property
    def SELF_WRITEABLE_FIELDS(self):
        return super().SELF_WRITEABLE_FIELDS + ["aidoo_cloud_project"]

    # ------------------------------------------------------------------
    # Provider adapter access
    # ------------------------------------------------------------------
    def _ai_provider(self, code):
        """Return the adapter AbstractModel for a provider code."""
        return self.env["aidoo.provider.%s" % code]

    def _ai_is_authenticated(self, provider):
        """True if this user has usable credentials for ``provider``."""
        self.ensure_one()
        return self._ai_provider(provider)._is_authenticated(self)

    def _ai_auth_states(self):
        """Map of provider code -> bool (connected?) for this user."""
        self.ensure_one()
        return {code: self._ai_is_authenticated(code) for code, _label in PROVIDERS}

    # ------------------------------------------------------------------
    # Effective AI tool set (single source of truth, see aidoo.session)
    # ------------------------------------------------------------------
    def _ai_allowed_tool_names(self):
        """Per-user tool selection, independent of zero-trust.

        Empty selection falls back to all read tools (write tools require an
        explicit grant)."""
        self.ensure_one()
        sel = self.aidoo_tool_ids
        return set(sel.mapped("name")) if sel else set(READ_TOOLS)

    def _ai_zero_trust(self):
        """Whether zero-trust (read-only) mode is active for this user."""
        self.ensure_one()
        if self.aidoo_zero_trust_mode != "inherit":
            return self.aidoo_zero_trust_mode == "on"
        raw = self.env["aidoo.session"]._config("zero_trust_default")
        return str2bool(raw, False) if raw is not None else False

    def _ai_effective_tools(self):
        """THE source of truth: tool names this user may actually invoke."""
        self.ensure_one()
        names = self._ai_allowed_tool_names() & set(ALL_TOOLS)
        if self._ai_zero_trust():
            names = {n for n in names if n in READONLY_TOOL_SET}
        return names

    def _ai_account_email(self):
        """The email the assistant should treat as the user's."""
        self.ensure_one()
        return self.email or self.partner_id.email or self.login or ""

    # ------------------------------------------------------------------
    # Identity / role context injected into the assistant's system prompt
    # ------------------------------------------------------------------
    def _ai_identity_prompt(self, effective_tools=None):
        """A concise, authoritative text block telling the model WHO the Odoo
        user is and what roles/permissions they hold.

        Carries no secrets — only identity, company, locale, privilege flags,
        application roles and the AI tool grant — so the model acts for, and
        within the rights of, this Odoo user."""
        self.ensure_one()
        if effective_tools is None:
            effective_tools = self._ai_effective_tools()

        email = self._ai_account_email() or "—"
        lines = [
            "--- ODOO USER CONTEXT (authoritative — this is who you are acting for) ---",
            "Name: %s" % (self.name or "—"),
            "Login: %s" % (self.login or "—"),
            "Email: %s" % email,
            "Odoo user id: %s" % self.id,
            "Company: %s" % (self.company_id.name or "—"),
        ]
        if len(self.company_ids) > 1:
            lines.append(
                "Allowed companies: %s" % ", ".join(self.company_ids.mapped("name")))
        lines.append("Language: %s" % (self.lang or "—"))
        lines.append("Timezone: %s" % (self.tz or "—"))

        if self._is_admin():
            priv = "Administrator (full access)"
        elif self._is_system():
            priv = "Settings/System access"
        elif self.has_group("base.group_user"):
            priv = "Internal user (not an administrator)"
        else:
            priv = "Portal/public user (limited access)"
        lines.append("Privilege level: %s" % priv)

        roles = sorted(
            "%s / %s" % (g.category_id.name, g.name)
            for g in self.groups_id if g.category_id
        )
        if roles:
            lines.append("Roles:")
            lines.extend("  - %s" % r for r in roles)
        else:
            lines.append("Roles: (none beyond base access)")

        if not effective_tools:
            lines.append("AI tools available to you: none.")
        else:
            writeable = sorted(set(effective_tools) - READONLY_TOOL_SET)
            if writeable:
                lines.append(
                    "AI tool grant: read-only data access PLUS write/action tools "
                    "(%s). Use Odoo business actions when available." % ", ".join(writeable))
            else:
                lines.append(
                    "AI tool grant: READ-ONLY. You cannot create, modify or delete "
                    "data for this user — only query and report.")
        if set(effective_tools) & set(WEB_TOOLS):
            lines.append(
                "Web access: you may use web fetch/search to retrieve and search "
                "public web content.")
        lines.append("--- END ODOO USER CONTEXT ---")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Logout (preferences button) — disconnect every provider
    # ------------------------------------------------------------------
    def action_aidoo_logout(self):
        for user in self:
            for code, _label in PROVIDERS:
                try:
                    user._ai_provider(code)._auth_logout(user)
                except Exception:  # noqa: BLE001
                    _logger.exception("aidoo: logout failed for provider %s", code)
        return True
