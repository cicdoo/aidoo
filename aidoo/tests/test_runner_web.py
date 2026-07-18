# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Web tools are CLI built-ins, not mcp__odoo__* bridge tools. These tests pin how
the Claude adapter wires a granted web tool: lifted from the deny list, added to
--allowedTools by bare name (WebFetch), and kept out of the bridge's tool list."""
import json
import sys
import tempfile

from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "aidoo")
class TestRunnerWeb(TransactionCase):

    def setUp(self):
        super().setUp()
        self.claude = self.env["aidoo.provider.claude"]
        self.scratch = tempfile.mkdtemp(prefix="aidoo_test_")

    def _ctx(self, allowed_tools):
        return {
            "scratch": self.scratch,
            "python_bin": sys.executable,
            "cli_path": "/usr/bin/claude",
            "model": "claude-sonnet-4-5",
            "max_turns": 30,
            "allowed_tools": allowed_tools,
            "identity": "ODOO USER CONTEXT",
            "is_first": True,
            "oauth_env": {"CLAUDE_CONFIG_DIR": "/tmp/none"},
            "cli_session_id": "00000000-0000-0000-0000-000000000000",
            "prompt": "hi",
            # _write_mcp_config keys:
            "base_url": "http://127.0.0.1:8069",
            "target_db": "testdb",
            "routing_sid": "sid",
            "token": "tok",
            "session_id": 1,
            "bridge_script": "/tmp/bridge.py",
            "excluded_models": [],
        }

    def test_granted_web_tool_lifts_deny_and_adds_bare_name(self):
        ctx = self._ctx(["orm_read", "web_fetch"])

        settings_path = self.claude._write_settings(ctx)
        with open(settings_path) as f:
            cfg = json.load(f)
        deny = cfg["permissions"]["deny"]
        self.assertNotIn("WebFetch", deny, "granted web tool must leave the deny list")
        self.assertIn("WebSearch", deny, "ungranted web tool stays denied")

        argv = self.claude._build_argv(
            ctx, "/tmp/mcp.json", settings_path, "/tmp/system_prompt.txt")
        allowed = argv[argv.index("--allowedTools") + 1].split(",")
        self.assertIn("WebFetch", allowed, "web tool goes in by bare built-in name")
        self.assertIn("mcp__odoo__orm_read", allowed)
        self.assertNotIn("mcp__odoo__web_fetch", allowed,
                         "web tool must not get the mcp__odoo__ prefix")

    def test_web_tool_excluded_from_bridge(self):
        ctx = self._ctx(["orm_read", "web_fetch"])
        mcp_path = self.claude._write_mcp_config(ctx)
        with open(mcp_path) as f:
            cfg = json.load(f)
        advertised = cfg["mcpServers"]["odoo"]["env"]["AI_ALLOWED_TOOLS"].split(",")
        self.assertIn("orm_read", advertised)
        self.assertNotIn("web_fetch", advertised,
                         "bridge serves only mcp__odoo__* tools, never web")

    def test_no_web_grant_keeps_builtins_denied(self):
        ctx = self._ctx(["orm_read"])
        settings_path = self.claude._write_settings(ctx)
        with open(settings_path) as f:
            cfg = json.load(f)
        deny = cfg["permissions"]["deny"]
        self.assertIn("WebFetch", deny)
        self.assertIn("WebSearch", deny)
