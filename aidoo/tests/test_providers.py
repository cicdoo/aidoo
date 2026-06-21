# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Cross-provider adapter contract tests.

Every provider adapter must: expose the right descriptor, build a runnable
command from a ctx (writing whatever config files its CLI needs), and normalize
its CLI's stream events into the common op format without ever raising on junk."""
import sys
import tempfile

from odoo.tests.common import TransactionCase, tagged

PROVIDER_CODES = ["claude", "codex", "gemini", "grok"]
# Providers that drive a CLI subprocess (implement _prepare_run/_normalize_event).
# Gemini is now NATIVE (in-process Code Assist) and implements _run_stream
# instead, so the CLI-shaped contract tests don't apply to it.
CLI_PROVIDER_CODES = ["claude", "codex", "grok"]


@tagged("post_install", "-at_install", "aidoo")
class TestProviders(TransactionCase):

    def setUp(self):
        super().setUp()
        self.scratch = tempfile.mkdtemp(prefix="aidoo_prov_")

    def _adapter(self, code):
        return self.env["aidoo.provider.%s" % code]

    def _ctx(self, code):
        return {
            "provider": code,
            "scratch": self.scratch,
            "uploads_dir": self.scratch + "/uploads",
            "python_bin": sys.executable,
            "cli_path": "/usr/bin/%s" % code,
            "model": "",
            "max_turns": 30,
            "allowed_tools": ["orm_read", "web_fetch"],
            "identity": "ODOO USER CONTEXT",
            "is_first": True,
            "cli_session_id": "00000000-0000-0000-0000-000000000000",
            "prompt": "hello world",
            "oauth_env": {"HOME": self.scratch, "CLAUDE_CONFIG_DIR": self.scratch},
            "base_url": "http://127.0.0.1:8069",
            "target_db": "testdb",
            "routing_sid": "sid",
            "token": "tok",
            "session_id": 1,
            "bridge_script": "/tmp/bridge.py",
            "excluded_models": [],
            "output_format": "",
        }

    def test_descriptors(self):
        for code in PROVIDER_CODES:
            d = self._adapter(code)._descriptor()
            self.assertEqual(d["code"], code)
            self.assertTrue(d["label"])
            self.assertIn(d["auth_mode"],
                          ("oauth_code", "device_poll", "import_or_key"))

    def test_prepare_run_shape(self):
        """Each CLI adapter returns a runnable spec and writes its config files."""
        for code in CLI_PROVIDER_CODES:
            prep = self._adapter(code)._prepare_run(self._ctx(code))
            for key in ("argv", "env", "cwd", "cleanup"):
                self.assertIn(key, prep, "%s missing %s" % (code, key))
            self.assertTrue(isinstance(prep["argv"], list) and prep["argv"],
                            "%s argv must be a non-empty list" % code)
            self.assertEqual(prep["argv"][0], "/usr/bin/%s" % code,
                             "%s argv must start with the resolved CLI path" % code)
            self.assertIn("PATH", prep["env"])

    def test_normalize_event_never_raises_on_junk(self):
        for code in CLI_PROVIDER_CODES:
            adapter = self._adapter(code)
            for junk in ({}, {"type": "totally-unknown"}, {"foo": "bar"}):
                ops = adapter._normalize_event(junk, {})
                self.assertIsInstance(ops, list, "%s must return a list" % code)

    def test_normalize_event_op_format(self):
        """Whatever ops an adapter emits must use the documented kinds/keys."""
        valid = {"session_id", "text", "tool_use", "tool_result", "done", "error"}
        sample = {
            "type": "assistant",
            "message": {"id": "m1", "content": [
                {"type": "text", "text": "hi"},
                {"type": "tool_use", "id": "t1",
                 "name": "mcp__odoo__orm_read", "input": {"model": "res.partner"}},
            ]},
        }
        for code in CLI_PROVIDER_CODES:
            ops = self._adapter(code)._normalize_event(sample, {})
            for op in ops:
                self.assertIn(op.get("op"), valid,
                              "%s emitted unknown op %r" % (code, op))

    def test_build_prompt_folds_context_for_cli_non_claude(self):
        """CLI non-Claude providers can't pass a system-prompt flag, so the system
        prompt + identity must be folded into the prompt text."""
        for code in ("codex", "grok"):
            session = self.env["aidoo.session"].create(
                {"user_id": self.env.user.id, "provider": code})
            prompt = self._adapter(code)._build_prompt(
                session, "What is 2+2?", [], True, "ODOO USER CONTEXT BLOCK")
            self.assertIn("ODOO USER CONTEXT BLOCK", prompt)
            self.assertIn("What is 2+2?", prompt)
            self.assertIn("Odoo", prompt)  # system prompt text present

    # ------------------------------------------------------------------
    # Native Gemini (in-process Code Assist) adapter
    # ------------------------------------------------------------------
    def test_gemini_is_native(self):
        """Gemini is native: it drives _run_stream, not a CLI subprocess."""
        adapter = self._adapter("gemini")
        self.assertTrue(adapter._is_native)
        with self.assertRaises(NotImplementedError):
            adapter._prepare_run(self._ctx("gemini"))

    def test_gemini_build_prompt_is_plain(self):
        """Native Gemini sends system+identity via systemInstruction, so the
        per-turn prompt is just the user's message (no folded system text)."""
        session = self.env["aidoo.session"].create(
            {"user_id": self.env.user.id, "provider": "gemini"})
        prompt = self._adapter("gemini")._build_prompt(
            session, "What is 2+2?", [], True, "ODOO USER CONTEXT BLOCK")
        self.assertIn("What is 2+2?", prompt)
        self.assertNotIn("ODOO USER CONTEXT BLOCK", prompt)

    def test_gemini_sanitize_schema(self):
        """MCP JSON-Schema -> Gemini OpenAPI subset: drop 'default', collapse
        union types, recurse into properties/items."""
        from odoo.addons.aidoo.models.providers.gemini import _sanitize_schema
        out = _sanitize_schema({
            "type": "object",
            "properties": {
                "model": {"type": "string"},
                "ids": {"type": "array", "items": {"type": "integer"},
                        "default": []},
                "action": {"type": ["integer", "string"]},
            },
            "required": ["model"],
        })
        self.assertEqual(out["type"], "object")
        self.assertNotIn("default", out["properties"]["ids"])
        self.assertEqual(out["properties"]["ids"]["type"], "array")
        self.assertEqual(out["properties"]["ids"]["items"]["type"], "integer")
        # Union type collapses to the first non-null member.
        self.assertEqual(out["properties"]["action"]["type"], "integer")
        self.assertEqual(out["required"], ["model"])

    def test_gemini_function_declarations_filtered(self):
        """Only granted Odoo tools are advertised; ungranted ones are withheld."""
        adapter = self._adapter("gemini")
        tools = adapter._function_declarations(["orm_read", "orm_search_read"])
        self.assertTrue(tools and "functionDeclarations" in tools[0])
        names = {d["name"] for d in tools[0]["functionDeclarations"]}
        self.assertEqual(names, {"orm_read", "orm_search_read"})
        for d in tools[0]["functionDeclarations"]:
            self.assertIn("parameters", d)
            self.assertEqual(d["parameters"]["type"], "object")
        # No grants -> no tool block.
        self.assertIsNone(adapter._function_declarations([]))

    # ------------------------------------------------------------------
    # Code Assist 429 / rate-limit handling
    # ------------------------------------------------------------------
    def test_codeassist_error_rate_limit_flag(self):
        from odoo.addons.aidoo.models.providers.codeassist import CodeAssistError
        self.assertTrue(CodeAssistError("x", status=429).is_rate_limit)
        self.assertFalse(CodeAssistError("x", status=500).is_rate_limit)
        self.assertFalse(CodeAssistError("x").is_rate_limit)
        # 404 (model unavailable) is flagged for fallback too.
        self.assertTrue(CodeAssistError("x", status=404).is_not_found)
        self.assertFalse(CodeAssistError("x", status=404).is_rate_limit)
        self.assertFalse(CodeAssistError("x", status=429).is_not_found)

    def test_codeassist_retry_delay_precedence(self):
        """Retry-After header > RetryInfo.retryDelay > body "reset after Ns" >
        exponential backoff; explicit hints capped, backoff bounded."""
        from odoo.addons.aidoo.models.providers.codeassist import (
            CodeAssistClient as C)
        self.assertEqual(C._retry_delay({"Retry-After": "12"}, "", 0), 12)
        ri = ('{"error":{"details":[{"@type":'
              '"type.googleapis.com/google.rpc.RetryInfo","retryDelay":"57s"}]}}')
        self.assertEqual(C._retry_delay({}, ri, 0), 58)
        self.assertEqual(C._retry_delay({}, "quota will reset after 30s.", 0), 31)
        big = '{"error":{"details":[{"@type":"x.RetryInfo","retryDelay":"999s"}]}}'
        self.assertEqual(C._retry_delay({}, big, 0), C._HINT_CAP_S)
        # Exponential fallback (no hint) is bounded by _MAX_DELAY_S (+ jitter).
        self.assertLessEqual(C._retry_delay({}, "no hint", 6),
                             C._MAX_DELAY_S * 1.2 + 0.01)

    def test_gemini_fallback_model_default(self):
        """The native Gemini adapter defaults its fallback model to flash."""
        self.assertEqual(
            self._adapter("gemini")._pconfig("fallback_model", "gemini-2.5-flash"),
            "gemini-2.5-flash")

    def test_gemini_sanitize_bare_array_to_string(self):
        """A bare array (no `items`) becomes a JSON-string param — Gemini flash
        rejects array properties without `items`. Typed arrays stay arrays."""
        from odoo.addons.aidoo.models.providers.gemini import _sanitize_schema
        out = _sanitize_schema({"type": "object", "properties": {
            "domain": {"type": "array", "default": []},
            "ids": {"type": "array", "items": {"type": "integer"}}}})
        self.assertEqual(out["properties"]["domain"]["type"], "string")
        self.assertIn("JSON", out["properties"]["domain"]["description"])
        self.assertEqual(out["properties"]["ids"]["type"], "array")
        self.assertEqual(out["properties"]["ids"]["items"]["type"], "integer")

    def test_tool_exec_coerces_json_string_args(self):
        """JSON-string array/object args (from models that can't type them) are
        parsed back to structures; real lists and plain strings pass through."""
        ex = self.env["aidoo.tool.exec"]
        coerced = ex._coerce_json_args({
            "model": "res.partner", "domain": '[["customer_rank", ">", 0]]',
            "fields": ["name"]})
        self.assertEqual(coerced["domain"], [["customer_rank", ">", 0]])
        self.assertEqual(coerced["fields"], ["name"])
        self.assertEqual(coerced["model"], "res.partner")
