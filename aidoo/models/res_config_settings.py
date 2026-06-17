# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
from odoo import fields, models

from .aidoo_session import PROVIDERS


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # --- Global ---
    aidoo_default_provider = fields.Selection(
        PROVIDERS, string="Default AI Provider",
        config_parameter="aidoo.default_provider", default="claude",
        help="Provider preselected when a user starts a new conversation.")

    # --- Per-provider: CLI path + model ---
    aidoo_claude_cli_path = fields.Char(
        string="Claude CLI Path", config_parameter="aidoo.claude.cli_path",
        help="Absolute path to the claude binary. Empty = auto-detect.")
    aidoo_claude_model = fields.Char(
        string="Claude Model", config_parameter="aidoo.claude.model",
        default="claude-sonnet-4-5")

    aidoo_codex_cli_path = fields.Char(
        string="Codex CLI Path", config_parameter="aidoo.codex.cli_path",
        help="Absolute path to the codex binary. Empty = auto-detect (PATH).")
    aidoo_codex_model = fields.Char(
        string="Codex Model", config_parameter="aidoo.codex.model", default="",
        help="Empty uses the ChatGPT account default.")

    aidoo_gemini_cli_path = fields.Char(
        string="Gemini CLI Path", config_parameter="aidoo.gemini.cli_path",
        help="Absolute path to the gemini binary. Empty = auto-detect (PATH).")
    aidoo_gemini_model = fields.Char(
        string="Gemini Model", config_parameter="aidoo.gemini.model", default="")
    aidoo_gemini_auth_mode = fields.Selection(
        [("oauth_import", "Import Google OAuth credentials (AI Ultra)"),
         ("api_key", "Gemini API key")],
        string="Gemini Authentication Mode",
        config_parameter="aidoo.gemini.auth_mode", default="oauth_import",
        help="How users connect Gemini: paste ~/.gemini/oauth_creds.json, or use "
             "a per-user / shared API key.")
    aidoo_gemini_api_key = fields.Char(
        string="Shared Gemini API Key", config_parameter="aidoo.gemini.api_key",
        help="Optional fallback key used when a user has no personal key.")
    aidoo_gemini_cloud_project = fields.Char(
        string="Shared Google Cloud Project",
        config_parameter="aidoo.gemini.cloud_project",
        help="Fallback GOOGLE_CLOUD_PROJECT when a user sets none.")
    aidoo_gemini_output_format = fields.Selection(
        [("stream-json", "Streaming JSON"), ("json", "Single JSON")],
        string="Gemini Output Format",
        config_parameter="aidoo.gemini.output_format", default="stream-json")

    aidoo_grok_cli_path = fields.Char(
        string="Grok CLI Path", config_parameter="aidoo.grok.cli_path",
        help="Absolute path to the grok binary. Empty = auto-detect (PATH).")
    aidoo_grok_model = fields.Char(
        string="Grok Model", config_parameter="aidoo.grok.model", default="")
    aidoo_grok_auth_mode = fields.Selection(
        [("oauth_import", "Import Grok auth.json"),
         ("api_key", "xAI API key")],
        string="Grok Authentication Mode",
        config_parameter="aidoo.grok.auth_mode", default="oauth_import")
    aidoo_grok_api_key = fields.Char(
        string="Shared xAI API Key", config_parameter="aidoo.grok.api_key")
    aidoo_grok_output_format = fields.Selection(
        [("streaming-json", "Streaming JSON"), ("json", "Single JSON")],
        string="Grok Output Format",
        config_parameter="aidoo.grok.output_format", default="streaming-json")

    # --- Shared run controls ---
    aidoo_max_turns = fields.Integer(
        string="Max Turns", config_parameter="aidoo.max_turns", default=30)
    aidoo_timeout_s = fields.Integer(
        string="Run Timeout (s)", config_parameter="aidoo.timeout_s", default=900)
    aidoo_max_concurrent_runs = fields.Integer(
        string="Max Concurrent Runs",
        config_parameter="aidoo.max_concurrent_runs", default=0,
        help="Maximum AI runs allowed at the same time across all users "
             "(each is a separate CLI subprocess). 0 = unlimited.")
    aidoo_max_upload_mb = fields.Integer(
        string="Max Upload Size (MB)", config_parameter="aidoo.max_upload_mb",
        default=15)
    aidoo_base_url = fields.Char(
        string="Odoo Host URL", config_parameter="aidoo.base_url",
        default="http://127.0.0.1:8069",
        help="Host URL the MCP bridge connects back to (loopback by default).")
    aidoo_db_name = fields.Char(
        string="Odoo Database", config_parameter="aidoo.db_name",
        help="Database the assistant connects to. Empty = current database.")
    aidoo_sql_enabled = fields.Boolean(
        string="Enable SQL Reporting Tool",
        config_parameter="aidoo.sql_enabled", default=True,
        help="Allow the read-only SQL tool (members of the AI SQL Analyst group).")
    aidoo_zero_trust_default = fields.Boolean(
        string="Zero-Trust by Default (read-only)",
        config_parameter="aidoo.zero_trust_default", default=False,
        help="Instance-wide default: when on, users left on 'Inherit' may only "
             "use read-only tools. A per-user override can still allow writes.")
    aidoo_action_method_patterns = fields.Char(
        string="AI Action Method Patterns",
        config_parameter="aidoo.action_methods", default="action_*,button_*",
        help="Comma-separated fnmatch patterns for methods orm_action/run_wizard "
             "may call. Private/dunder methods are always blocked.")
    # Non-stored: persisted as CSV in aidoo.excluded_models (see get/set_values).
    aidoo_excluded_model_ids = fields.Many2many(
        "ir.model", string="Models Hidden from AI",
        help="The AI Assistant will refuse to introspect, read, or write these "
             "models for every user.")
    # Non-stored: persisted as CSV of ids in aidoo.server_action_ids.
    aidoo_allowed_server_action_ids = fields.Many2many(
        "ir.actions.server", string="AI-Runnable Server Actions",
        help="Only these server actions may be run via run_server_action. "
             "Empty = none. A 'code' server action runs arbitrary Python, so "
             "only allowlist actions you trust.")

    def get_values(self):
        res = super().get_values()
        names = self.env["aidoo.session"]._ai_excluded_models()
        models = self.env["ir.model"].search([("model", "in", list(names))])
        res["aidoo_excluded_model_ids"] = [(6, 0, models.ids)]
        sa_ids = self.env["aidoo.session"]._ai_allowed_server_action_ids()
        res["aidoo_allowed_server_action_ids"] = [(6, 0, list(sa_ids))]
        return res

    def set_values(self):
        super().set_values()
        names = ",".join(sorted(self.aidoo_excluded_model_ids.mapped("model")))
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("aidoo.excluded_models", names)
        ICP.set_param("aidoo.server_action_ids",
                      ",".join(map(str, sorted(self.aidoo_allowed_server_action_ids.ids))))
