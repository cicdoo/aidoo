# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Custom LLM provider — an Anthropic-compatible gateway driven by the Claude CLI.

Mirrors the ``env`` block of a Claude Code ``settings.local.json``
(``ANTHROPIC_BASE_URL``, ``ANTHROPIC_AUTH_TOKEN``, ``ANTHROPIC_MODEL``,
``ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL``, ``CLAUDE_CODE_SUBAGENT_MODEL``).
All pipeline code (MCP bridge, tool gating, stream-json parsing) is inherited
from the Claude adapter; only auth and the CLI environment differ. There is no
OAuth login: the token is a shared system parameter (``aidoo.custom.api_key``)
or a per-user file (mode 0600 under ``.custom``, never the DB).
"""
import os

from odoo import _
from odoo.exceptions import UserError

from .claude import AidooProviderClaude

_TIER_ENV = (
    ("model", "ANTHROPIC_MODEL"),
    ("opus_model", "ANTHROPIC_DEFAULT_OPUS_MODEL"),
    ("sonnet_model", "ANTHROPIC_DEFAULT_SONNET_MODEL"),
    ("haiku_model", "ANTHROPIC_DEFAULT_HAIKU_MODEL"),
    ("subagent_model", "CLAUDE_CODE_SUBAGENT_MODEL"),
)


class AidooProviderCustom(AidooProviderClaude):
    _name = "aidoo.provider.custom"
    _inherit = "aidoo.provider.claude"
    _description = "Aidoo Provider — Custom LLM (Anthropic-compatible)"

    _provider_code = "custom"
    _provider_label = "Custom LLM"
    _config_dir_name = ".custom"
    # The CLI requires --model; the "sonnet" alias resolves through
    # ANTHROPIC_DEFAULT_SONNET_MODEL when no explicit model is configured.
    _default_model = "sonnet"
    _auth_ui_mode = "import_or_key"
    _auth_key_supported = True
    _auth_import_help = (
        "Paste the auth token of your Anthropic-compatible gateway "
        "(ANTHROPIC_AUTH_TOKEN) in the field below.")

    # ------------------------------------------------------------------
    # Config (provider-scoped only: aidoo.base_url is the Odoo host URL)
    # ------------------------------------------------------------------
    def _param(self, key):
        val = self.env["ir.config_parameter"].sudo().get_param(
            "aidoo.custom.%s" % key)
        return (val or "").strip()

    def _base_url(self):
        return self._param("base_url")

    def _configured(self):
        return bool(self._base_url())

    def _descriptor(self):
        desc = super()._descriptor()
        desc["label"] = self._param("label") or self._provider_label
        att = self.env["res.config.settings"]._aidoo_custom_icon_attachment()
        if att:
            desc["icon"] = "/web/image/%d?unique=%s" % (att.id, att.checksum)
        return desc

    # ------------------------------------------------------------------
    # Token: per-user file wins over the shared system parameter
    # ------------------------------------------------------------------
    def _api_key_path(self, user):
        return os.path.join(self._config_dir(user), "api_key")

    def _api_key(self, user):
        try:
            with open(self._api_key_path(user), "r") as f:
                key = f.read().strip()
            if key:
                return key
        except OSError:
            pass
        return self._param("api_key")

    def _is_authenticated(self, user):
        return bool(self._api_key(user)) and self._configured()

    # ------------------------------------------------------------------
    # CLI env
    # ------------------------------------------------------------------
    def _oauth_env(self, user):
        env = super()._oauth_env(user)
        env["ANTHROPIC_BASE_URL"] = self._base_url()
        env["ANTHROPIC_AUTH_TOKEN"] = self._api_key(user)
        for key, var in _TIER_ENV:
            val = self._param(key)
            if val:
                env[var] = val
        return env

    def _sync_cli_account(self, user):
        return False

    # ------------------------------------------------------------------
    # Authentication — token only, no OAuth
    # ------------------------------------------------------------------
    def _auth_start(self, user):
        raise UserError(_("Custom LLM has no login flow. Paste your gateway token."))

    def _auth_complete(self, user, pasted_code):
        raise UserError(_("Custom LLM has no login flow. Paste your gateway token."))

    def _auth_set_key(self, user, key):
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
        blob = (blob or "").strip()
        if not blob:
            raise UserError(_("Paste your gateway auth token."))
        return self._auth_set_key(user, blob)

    def _auth_logout(self, user):
        try:
            os.remove(self._api_key_path(user))
        except OSError:
            pass
        return {"authenticated": False}
