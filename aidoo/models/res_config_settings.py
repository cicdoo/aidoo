# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
from odoo import api, fields, models

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

    # Codex is native (no CLI binary); the legacy cli_path field is kept out of
    # the UI but not deleted, so an existing configured value survives upgrades.
    aidoo_codex_cli_path = fields.Char(
        string="Codex CLI Path (legacy)", config_parameter="aidoo.codex.cli_path",
        help="Unused by the native Codex provider. Kept for the legacy CLI "
             "adapter only.")
    aidoo_codex_model = fields.Char(
        string="Codex Model", config_parameter="aidoo.codex.model",
        default="gpt-5.5",
        help="Model id on the ChatGPT Codex backend (e.g. gpt-5.5, gpt-5.4, "
             "gpt-5.4-mini). Codex-branded ids (gpt-*-codex) are NOT accepted "
             "with a ChatGPT account. Empty falls back to gpt-5.5.")
    aidoo_codex_base_url = fields.Char(
        string="Codex Base URL", config_parameter="aidoo.codex.base_url",
        help="ChatGPT Codex backend base URL. Empty = "
             "https://chatgpt.com/backend-api/codex.")
    aidoo_codex_rpm_limit = fields.Integer(
        string="Codex Requests/min Cap",
        config_parameter="aidoo.codex.rpm_limit", default=0,
        help="Proactive client-side cap on Codex requests per minute (per "
             "account, per Odoo worker). 0 disables.")
    # MCP bridge timeouts: only meaningful for the legacy CLI adapter
    # (codex_cli.py, unregistered) — the native provider runs tools in-process
    # and never spawns the bridge. Kept (out of the UI) like cli_path above.
    aidoo_codex_mcp_startup_timeout_sec = fields.Integer(
        string="Codex MCP Startup Timeout (s)",
        config_parameter="aidoo.codex.mcp_startup_timeout_sec", default=30,
        help="Legacy CLI adapter only. How long Codex waits for the odoo MCP "
             "bridge to complete its startup handshake before giving up and "
             "dropping its tools.")
    aidoo_codex_mcp_tool_timeout_sec = fields.Integer(
        string="Codex MCP Tool Timeout (s)",
        config_parameter="aidoo.codex.mcp_tool_timeout_sec", default=120,
        help="Legacy CLI adapter only. Per-call ceiling for odoo MCP tools "
             "under Codex (kept above the bridge's own 60s HTTP timeout).")

    aidoo_gemini_cli_path = fields.Char(
        string="Gemini CLI Path", config_parameter="aidoo.gemini.cli_path",
        help="Absolute path to the gemini binary. Empty = auto-detect (PATH).")
    aidoo_gemini_model = fields.Char(
        string="Gemini Model", config_parameter="aidoo.gemini.model", default="")
    aidoo_gemini_fallback_model = fields.Char(
        string="Gemini Fallback Model",
        config_parameter="aidoo.gemini.fallback_model", default="gemini-2.5-flash",
        help="Lighter model to fall back to when the primary stays rate-limited "
             "(429). It uses a separate quota bucket, so replies recover instead "
             "of failing. Empty disables fallback.")
    aidoo_gemini_rpm_limit = fields.Integer(
        string="Gemini Requests/min Cap",
        config_parameter="aidoo.gemini.rpm_limit", default=120,
        help="Proactive client-side cap on Code Assist requests per minute "
             "(per project, per Odoo worker), to stay under the tier's quota. "
             "0 disables. Note: rate limits are also per-model — pro is far "
             "lower than flash regardless of this cap.")
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
        help="Code Assist project (GCP project id) used when a user sets no "
             "personal one. Required to use a Gemini Code Assist ENTERPRISE "
             "tier — the project where Code Assist is enabled and the per-user "
             "license assigned. Empty = Google's auto individual/standard "
             "project. A personal Gmail only ever gets the standard tier.")
    aidoo_gemini_output_format = fields.Selection(
        [("stream-json", "Streaming JSON"), ("json", "Single JSON")],
        string="Gemini Output Format",
        config_parameter="aidoo.gemini.output_format", default="stream-json")

    aidoo_grok_cli_path = fields.Char(
        string="Grok CLI Path", config_parameter="aidoo.grok.cli_path",
        help="Legacy CLI adapter only. Absolute path to the grok binary.")
    aidoo_grok_model = fields.Char(
        string="Grok Model", config_parameter="aidoo.grok.model", default="",
        help="xAI model id. Empty = grok-4.7.")
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
        config_parameter="aidoo.grok.output_format", default="streaming-json",
        help="Legacy CLI adapter only.")

    aidoo_zai_model = fields.Char(
        string="Z.AI Model", config_parameter="aidoo.zai.model",
        default="glm-4.6",
        help="GLM model id used for the coding plan (e.g. glm-4.6, glm-5.2).")
    aidoo_zai_base_url = fields.Char(
        string="Z.AI Base URL", config_parameter="aidoo.zai.base_url",
        default="https://api.z.ai/api/coding/paas/v4",
        help="GLM Coding Plan endpoint (OpenAI-compatible). Use the China "
             "platform variant if your subscription is on open.bigmodel.cn.")
    aidoo_zai_api_key = fields.Char(
        string="Shared Z.AI API Key", config_parameter="aidoo.zai.api_key",
        help="Optional fallback key used when a user has no personal key.")

    aidoo_antigravity_enabled = fields.Boolean(
        string="Enable Antigravity (experimental)",
        config_parameter="aidoo.antigravity.enabled", default=False,
        help="Show the experimental Antigravity (agy) provider in the picker. "
             "Its web tools cannot be sandboxed (ungated internet access) — do "
             "not enable for zero-trust deployments. See SECURITY.md.")
    aidoo_antigravity_cli_path = fields.Char(
        string="Antigravity CLI Path",
        config_parameter="aidoo.antigravity.cli_path",
        help="Absolute path to the agy binary. Empty = auto-detect (PATH).")
    aidoo_antigravity_model = fields.Char(
        string="Antigravity Model", config_parameter="aidoo.antigravity.model",
        default="", help="Empty uses the account default model.")

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
    aidoo_dispatch_interval_minutes = fields.Integer(
        string="Scheduler Check Interval (minutes)",
        config_parameter="aidoo.dispatch_interval_minutes", default=15,
        help="How often Aidoo checks for and launches due scheduled agents. "
             "This is the smallest effective period for a scheduled agent — an "
             "agent set to run more frequently than this still only fires this "
             "often.")
    aidoo_max_upload_mb = fields.Integer(
        string="Max Upload Size (MB)", config_parameter="aidoo.max_upload_mb",
        default=15)
    aidoo_base_url = fields.Char(
        string="Odoo Host URL", config_parameter="aidoo.base_url",
        default="http://127.0.0.1:8069",
        help="Host URL the MCP bridge connects back to (loopback by default).")
    aidoo_mcp_enabled = fields.Boolean(
        string="Enable Remote MCP Server",
        config_parameter="aidoo.mcp_enabled", default=True,
        help="Enabled by default. Exposes POST /aidoo/mcp (with OAuth sign-in for ChatGPT/Claude "
             "connectors) so external MCP clients can use the assistant's tools, or a user API "
             "key of scope 'aidoo_mcp'.")
    aidoo_mcp_url = fields.Char(
        string="MCP Endpoint URL", compute="_compute_aidoo_mcp_url")
    aidoo_oauth_metadata_url = fields.Char(
        string="OAuth Metadata URL", compute="_compute_aidoo_mcp_url")
    aidoo_db_name = fields.Char(
        string="Odoo Database", config_parameter="aidoo.db_name",
        help="Database the assistant connects to. Empty = current database.")
    aidoo_sql_enabled = fields.Boolean(
        string="Enable SQL Reporting Tool",
        config_parameter="aidoo.sql_enabled", default=True,
        help="Allow the read-only SQL tool (members of the AI SQL Analyst group).")
    aidoo_sql_table_denylist = fields.Char(
        string="SQL Table Denylist",
        config_parameter="aidoo.sql_table_denylist", default="",
        help="Comma/space-separated table-name patterns (fnmatch globs, e.g. "
             "account_*, hr_*, *_payslip) that the SQL reporting tool may NOT "
             "read from. Raw SQL bypasses Odoo record rules, so use this to keep "
             "sensitive tables (invoices, payroll…) out of reach even for AI SQL "
             "Analyst members. Empty = no extra restriction.")
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
    aidoo_action_method_deny_patterns = fields.Char(
        string="AI Denied Action Methods",
        config_parameter="aidoo.action_methods_deny",
        default="action_archive,action_unarchive",
        help="Comma-separated fnmatch patterns orm_action/run_wizard may never "
             "call, even when the allowlist matches. A change request can still "
             "propose them for a person to approve. Empty = the default.")
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
        cron = self.env.ref("aidoo.cron_ai_scheduled", raise_if_not_found=False)
        if cron and cron.interval_type == "minutes":
            res["aidoo_dispatch_interval_minutes"] = cron.interval_number
        return res

    def set_values(self):
        super().set_values()
        names = ",".join(sorted(self.aidoo_excluded_model_ids.mapped("model")))
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("aidoo.excluded_models", names)
        ICP.set_param("aidoo.server_action_ids",
                      ",".join(map(str, sorted(self.aidoo_allowed_server_action_ids.ids))))
        # Push the dispatcher interval onto the real cron so it takes effect.
        minutes = max(int(self.aidoo_dispatch_interval_minutes or 15), 1)
        cron = self.env.ref("aidoo.cron_ai_scheduled", raise_if_not_found=False)
        if cron:
            cron.sudo().write({
                "interval_number": minutes, "interval_type": "minutes"})

    @api.depends("aidoo_mcp_enabled")
    def _compute_aidoo_mcp_url(self):
        base = (self.env["ir.config_parameter"].sudo()
                .get_param("web.base.url", "") or "").rstrip("/")
        for rec in self:
            rec.aidoo_mcp_url = base + "/aidoo/mcp"
            rec.aidoo_oauth_metadata_url = (
                base + "/.well-known/oauth-authorization-server")
