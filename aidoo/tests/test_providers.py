# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Cross-provider adapter contract tests.

Every provider adapter must: expose the right descriptor, build a runnable
command from a ctx (writing whatever config files its CLI needs), and normalize
its CLI's stream events into the common op format without ever raising on junk."""
import json
import os
import sys
import tempfile
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

PROVIDER_CODES = ["claude", "codex", "gemini", "grok", "zai"]
# Providers that drive a CLI subprocess (implement _prepare_run/_normalize_event).
# Codex, Gemini and z.ai are NATIVE (in-process) and implement _run_stream
# instead, so the CLI-shaped contract tests don't apply to them.
CLI_PROVIDER_CODES = ["claude"]


@tagged("post_install", "-at_install", "aidoo")
class TestProviders(TransactionCase):

    def setUp(self):
        super().setUp()
        self.scratch = tempfile.mkdtemp(prefix="aidoo_prov_")

    def _adapter(self, code):
        return self.env["aidoo.provider.%s" % code]

    def _no_audit(self):
        """Stub the separate-cursor audit writer: it can't see the uncommitted
        test session and would raise an FK error. Audit is covered by
        test_tool_log; here we only exercise the native tool loop."""
        return patch.object(type(self.env["aidoo.tool_log"]), "_record",
                            lambda self, vals: None)

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

    def test_system_prompt_handles_deferred_mcp_tools(self):
        """Newer CLIs (e.g. Codex 0.144+) defer MCP tools behind a tool-search
        layer instead of listing them directly. The system prompt must tell the
        model to search for them, and must NOT emit the "not available" fallback
        merely because the tools aren't listed up-front — only after searching."""
        from odoo.addons.aidoo.models.providers.base import SYSTEM_PROMPT
        self.assertIn("tool_search", SYSTEM_PROMPT)
        self.assertIn("mcp__odoo__", SYSTEM_PROMPT)
        self.assertIn("after actually searching", SYSTEM_PROMPT)

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
    # Native z.ai (in-process GLM Coding Plan) adapter
    # ------------------------------------------------------------------
    def test_zai_is_native(self):
        """z.ai is native: it drives _run_stream, not a CLI subprocess."""
        adapter = self._adapter("zai")
        self.assertTrue(adapter._is_native)
        with self.assertRaises(NotImplementedError):
            adapter._prepare_run(self._ctx("zai"))

    def test_zai_tool_specs_openai_shape(self):
        """Granted tools are advertised in OpenAI function form; ungranted
        ones are withheld; no grants -> no tool block."""
        adapter = self._adapter("zai")
        specs = adapter._tool_specs(["orm_read", "orm_search_read"])
        self.assertTrue(specs)
        names = {s["function"]["name"] for s in specs}
        self.assertEqual(names, {"orm_read", "orm_search_read"})
        for s in specs:
            self.assertEqual(s["type"], "function")
            self.assertIn("parameters", s["function"])
        self.assertIsNone(adapter._tool_specs([]))

    def test_zai_build_prompt_is_plain(self):
        """Native z.ai sends system+identity via the system message, so the
        per-turn prompt is just the user's message (no folded system text)."""
        session = self.env["aidoo.session"].create(
            {"user_id": self.env.user.id, "provider": "zai"})
        prompt = self._adapter("zai")._build_prompt(
            session, "What is 2+2?", [], True, "ODOO USER CONTEXT BLOCK")
        self.assertIn("What is 2+2?", prompt)
        self.assertNotIn("ODOO USER CONTEXT BLOCK", prompt)

    def test_zai_accumulate_tool_calls(self):
        """Streamed tool_call fragments (keyed by index) concatenate arguments
        and keep the first id/name seen."""
        from odoo.addons.aidoo.models.providers.zai_client import ZaiClient
        acc = {}
        ZaiClient.accumulate_tool_calls(acc, [
            {"index": 0, "id": "call_1",
             "function": {"name": "orm_read", "arguments": '{"mod'}}])
        ZaiClient.accumulate_tool_calls(acc, [
            {"index": 0, "function": {"arguments": 'el": "res.partner"}'}}])
        self.assertEqual(acc[0]["id"], "call_1")
        self.assertEqual(acc[0]["name"], "orm_read")
        self.assertEqual(acc[0]["arguments"], '{"model": "res.partner"}')

    # ------------------------------------------------------------------
    # Native Grok (xAI API, inherits the z.ai loop) adapter
    # ------------------------------------------------------------------
    def test_grok_is_native(self):
        adapter = self._adapter("grok")
        self.assertTrue(adapter._is_native)
        self.assertEqual(adapter._base_url(), "https://api.x.ai/v1")
        with self.assertRaises(NotImplementedError):
            adapter._prepare_run(self._ctx("grok"))

    def _grok_auth(self, adapter, entry):
        """Write a `grok login` auth.json for the current user into a temp home."""
        self.env["ir.config_parameter"].sudo().set_param(
            "aidoo.grok.home_root", self.scratch)
        path = adapter._auth_path(self.env.user)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"https://auth.x.ai::cid": entry}, f)
        return path

    def test_grok_refresh_persists_rotated_token(self):
        """A near-expiry login is refreshed once; the new key, rotated refresh
        token and expiry are written back and unknown fields are kept."""
        from odoo.addons.aidoo.models.providers import grok
        adapter = self._adapter("grok")
        path = self._grok_auth(adapter, {
            "key": "old", "refresh_token": "rt1", "email": "a@b.c",
            "oidc_issuer": "https://auth.x.ai", "oidc_client_id": "cid",
            "expires_at": "2000-01-01T00:00:00Z"})
        seen = {}

        def fake_refresh(self, entry):
            seen["rt"] = entry["refresh_token"]
            return dict(entry, key="new", refresh_token="rt2",
                        expires_at="2999-01-01T00:00:00.000000Z")

        with patch.object(type(adapter), "_refresh_entry", new=fake_refresh):
            self.assertEqual(adapter._bearer_token(self.env.user), "new")
            # Now fresh: no second refresh.
            self.assertEqual(adapter._bearer_token(self.env.user), "new")
        self.assertEqual(seen["rt"], "rt1")
        with open(path) as f:
            entry = json.load(f)["https://auth.x.ai::cid"]
        self.assertEqual(entry["refresh_token"], "rt2")
        self.assertEqual(entry["email"], "a@b.c")
        self.assertTrue(grok._parse_expiry(entry["expires_at"]) > 4e9)

    def test_grok_expired_login_without_refresh_is_disconnected(self):
        adapter = self._adapter("grok")
        self._grok_auth(adapter, {"key": "old",
                                  "expires_at": "2000-01-01T00:00:00Z"})
        self.env["ir.config_parameter"].sudo().set_param("aidoo.grok.api_key", "")
        self.assertFalse(adapter._is_authenticated(self.env.user))

    def test_grok_import_rejects_entry_without_key(self):
        adapter = self._adapter("grok")
        self.env["ir.config_parameter"].sudo().set_param(
            "aidoo.grok.home_root", self.scratch)
        with self.assertRaises(UserError):
            adapter._auth_import(self.env.user, '{"x": {"email": "a@b.c"}}')

    def test_grok_native_tool_loop(self):
        from odoo.addons.aidoo.models.providers import zai_client
        user, user_env, session, adapter, ctx = self._native_user_env("grok")
        self.env["ir.config_parameter"].sudo().set_param(
            "aidoo.grok.api_key", "test-key")
        args = ('{"model": "res.users", "ids": [%s], "fields": ["login"]}'
                % user.id)
        rounds = [
            [{"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_1",
                 "function": {"name": "orm_read", "arguments": args}}]}}]}],
            [{"choices": [{"delta": {"content": "Your login is "
                                     "aidoo_native_grok."}}]}],
        ]
        calls = {"n": 0, "base": None}

        def fake_stream_chat(self, model, messages, **kw):
            calls["base"] = self._base
            i = calls["n"]
            calls["n"] += 1
            for chunk in rounds[i]:
                yield chunk

        with patch.object(zai_client.ZaiClient, "stream_chat",
                          new=fake_stream_chat), self._no_audit():
            ops = list(adapter._run_stream(user_env, session, ctx))

        self.assertEqual(calls["n"], 2)
        self.assertEqual(calls["base"], "https://api.x.ai/v1")
        tool_result = next(o for o in ops if o["op"] == "tool_result")
        self.assertFalse(tool_result["is_error"])
        self.assertIn("aidoo_native_grok", tool_result["text"])

    # ------------------------------------------------------------------
    # Native Codex (in-process ChatGPT Codex backend) adapter
    # ------------------------------------------------------------------
    def test_codex_is_native(self):
        """Codex is native: it drives _run_stream, not a CLI subprocess."""
        adapter = self._adapter("codex")
        self.assertTrue(adapter._is_native)
        with self.assertRaises(NotImplementedError):
            adapter._prepare_run(self._ctx("codex"))

    def test_codex_tool_specs_flat_responses_shape(self):
        """Responses-API function tools are FLAT (name/parameters at the top
        level, no nested `function` object); ungranted tools withheld."""
        adapter = self._adapter("codex")
        specs = adapter._tool_specs(["orm_read", "orm_search_read"])
        self.assertTrue(specs)
        names = {s["name"] for s in specs}
        self.assertEqual(names, {"orm_read", "orm_search_read"})
        for s in specs:
            self.assertEqual(s["type"], "function")
            self.assertNotIn("function", s)
            self.assertIn("parameters", s)
            self.assertFalse(s["strict"])
        self.assertIsNone(adapter._tool_specs([]))

    def test_codex_read_attachment(self):
        import os
        import tempfile
        adapter = self._adapter("codex")
        with tempfile.TemporaryDirectory() as tmp:
            up = os.path.join(tmp, "uploads")
            os.makedirs(up)
            txt = os.path.join(up, "a.txt")
            with open(txt, "w") as f:
                f.write("hello")
            with open(os.path.join(tmp, "secret.txt"), "w") as f:
                f.write("x")
            os.symlink(os.path.join(tmp, "secret.txt"), os.path.join(up, "l.txt"))
            with open(os.path.join(up, "v.mp3"), "wb") as f:
                f.write(b"x")
            ctx = {"uploads_dir": up}
            self.assertEqual(adapter._read_attachment(ctx, txt)[0], {"content": "hello"})
            self.assertIn("error", adapter._read_attachment(ctx, up + "/../secret.txt")[0])
            self.assertIn("error", adapter._read_attachment(ctx, os.path.join(up, "l.txt"))[0])
            self.assertIn("error", adapter._read_attachment(ctx, os.path.join(up, "v.mp3"))[0])
        spec = adapter._attachment_tool_spec()
        self.assertEqual(spec["name"], "read_attachment")
        self.assertNotIn("function", spec)

    def test_codex_build_prompt_is_plain(self):
        """Native Codex sends system+identity via `instructions`, so the
        per-turn prompt is just the user's message (no folded system text)."""
        session = self.env["aidoo.session"].create(
            {"user_id": self.env.user.id, "provider": "codex"})
        prompt = self._adapter("codex")._build_prompt(
            session, "What is 2+2?", [], True, "ODOO USER CONTEXT BLOCK")
        self.assertIn("What is 2+2?", prompt)
        self.assertNotIn("ODOO USER CONTEXT BLOCK", prompt)

    def test_codex_client_error_rate_limit_flag(self):
        from odoo.addons.aidoo.models.providers.codex_client import CodexError
        self.assertTrue(CodexError("x", status=429).is_rate_limit)
        self.assertFalse(CodexError("x", status=500).is_rate_limit)
        self.assertFalse(CodexError("x").is_rate_limit)

    def test_codex_auth_import_normalizes(self):
        """A bare tokens object is wrapped into the full auth.json shape; a blob
        without both tokens is rejected."""
        import json as _json
        from odoo.exceptions import UserError as UE
        adapter = self._adapter("codex")
        user = self.env.user
        with patch.object(type(adapter), "_write_user_file",
                          new=lambda self, u, path, content:
                          written.update(json=_json.loads(content))) :
            written = {}
            res = adapter._auth_import(user, _json.dumps(
                {"access_token": "a.t", "refresh_token": "r.t"}))
            self.assertTrue(res["authenticated"])
            self.assertEqual(written["json"]["tokens"]["access_token"], "a.t")
            self.assertIn("OPENAI_API_KEY", written["json"])
            with self.assertRaises(UE):
                adapter._auth_import(user, _json.dumps({"access_token": "a"}))
            with self.assertRaises(UE):
                adapter._auth_import(user, "not json")

    def test_codex_native_tool_loop(self):
        from odoo.addons.aidoo.models.providers import codex, codex_client
        user, user_env, session, adapter, ctx = self._native_user_env("codex")
        args = ('{"model": "res.users", "ids": [%s], "fields": ["login"]}'
                % user.id)
        rounds = [
            # Round 0: the model asks to call orm_read.
            [{"type": "response.output_item.done",
              "item": {"type": "function_call", "call_id": "call_1",
                       "name": "orm_read", "arguments": args}}],
            # Round 1: the model answers in plain text and stops.
            [{"type": "response.output_text.delta",
              "delta": "Your login is aidoo_native_codex."},
             {"type": "response.output_item.done",
              "item": {"type": "message", "role": "assistant",
                       "content": [{"type": "output_text",
                                    "text": "Your login is aidoo_native_codex."}]}},
             {"type": "response.completed", "response": {}}],
        ]
        calls = {"n": 0, "inputs": []}

        def fake_stream_responses(self, model, input_items, **kw):
            i = calls["n"]
            calls["n"] += 1
            calls["inputs"].append(list(input_items))
            for evt in rounds[i]:
                yield evt

        with patch.object(codex.AidooProviderCodex, "_load_auth",
                          new=lambda self, u: {"tokens": {
                              "access_token": "x", "refresh_token": "y"}}), \
                patch.object(codex_client.CodexClient, "stream_responses",
                             new=fake_stream_responses), self._no_audit():
            ops = list(adapter._run_stream(user_env, session, ctx))

        kinds = [o["op"] for o in ops]
        self.assertIn("session_id", kinds)  # client-minted conversation id
        self.assertIn("tool_use", kinds)
        self.assertIn("tool_result", kinds)
        self.assertIn("text", kinds)
        self.assertEqual(calls["n"], 2)
        tool_use = next(o for o in ops if o["op"] == "tool_use")
        self.assertEqual(tool_use["name"], "orm_read")
        self.assertIn("aidoo_native_codex",
                      next(o for o in ops if o["op"] == "tool_result")["text"])
        # Round 1's input replays the function_call item AND its output.
        round2 = calls["inputs"][1]
        types = [i.get("type") for i in round2]
        self.assertIn("function_call", types)
        self.assertIn("function_call_output", types)

    def test_codex_native_loop_stops_at_round_cap(self):
        from odoo.addons.aidoo.models.providers import codex, codex_client
        user, user_env, session, adapter, ctx = self._native_user_env("codex")
        ctx["max_turns"] = 3
        args = '{"model": "res.users", "ids": [%s]}' % user.id
        calls = {"n": 0}

        def always_calls_tool(self, model, input_items, **kw):
            calls["n"] += 1
            yield {"type": "response.output_item.done",
                   "item": {"type": "function_call", "call_id": "c",
                            "name": "orm_read", "arguments": args}}

        with patch.object(codex.AidooProviderCodex, "_load_auth",
                          new=lambda self, u: {"tokens": {
                              "access_token": "x", "refresh_token": "y"}}), \
                patch.object(codex_client.CodexClient, "stream_responses",
                             new=always_calls_tool), self._no_audit():
            ops = list(adapter._run_stream(user_env, session, ctx))

        self.assertEqual(calls["n"], 3)  # capped at max_turns
        self.assertTrue(any("stopped after" in o.get("text", "")
                            for o in ops if o["op"] == "text"))

    def test_codex_dev_fallback_on_instructions_400(self):
        """A 400 on round 0 retries once with the system text demoted to a
        developer message and minimal instructions."""
        from odoo.addons.aidoo.models.providers import codex, codex_client
        user, user_env, session, adapter, ctx = self._native_user_env("codex")
        calls = {"n": 0, "kw": []}

        def first_400_then_text(self, model, input_items, **kw):
            calls["n"] += 1
            calls["kw"].append((list(input_items), kw))
            if calls["n"] == 1:
                raise codex_client.CodexError("bad instructions", status=400)
            yield {"type": "response.output_text.delta", "delta": "hi"}

        with patch.object(codex.AidooProviderCodex, "_load_auth",
                          new=lambda self, u: {"tokens": {
                              "access_token": "x", "refresh_token": "y"}}), \
                patch.object(codex_client.CodexClient, "stream_responses",
                             new=first_400_then_text), self._no_audit():
            ops = list(adapter._run_stream(user_env, session, ctx))

        self.assertEqual(calls["n"], 2)
        self.assertFalse(any(o["op"] == "error" for o in ops))
        retry_items, retry_kw = calls["kw"][1]
        self.assertEqual(retry_items[0]["role"], "developer")
        self.assertIn("ODOO USER CONTEXT",
                      retry_items[0]["content"][0]["text"])
        self.assertNotIn("ODOO USER CONTEXT", retry_kw["instructions"])

    # ------------------------------------------------------------------
    # Antigravity (experimental, opt-in, text-only CLI provider)
    # ------------------------------------------------------------------
    def test_antigravity_descriptor_and_flags(self):
        adapter = self.env["aidoo.provider.antigravity"]
        d = adapter._descriptor()
        self.assertEqual(d["code"], "antigravity")
        self.assertTrue(d["label"])
        self.assertIn(d["auth_mode"],
                      ("oauth_code", "device_poll", "import_or_key"))
        # It drives a CLI (not the native in-process path) and is gated as
        # experimental/opt-in so it never shows up by default.
        self.assertFalse(adapter._is_native)
        self.assertTrue(adapter._experimental)

    def test_antigravity_normalize_event_is_text_only(self):
        # agy emits no structured stream; _normalize_event must stay a no-op and
        # never raise, whatever junk it is handed.
        adapter = self.env["aidoo.provider.antigravity"]
        for junk in ({}, {"type": "x"}, {"foo": "bar"}):
            self.assertEqual(adapter._normalize_event(junk, {}), [])

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

    # ------------------------------------------------------------------
    # Native tool-calling loop — the in-process _run_stream generators drive a
    # full streaming -> tool_use -> tool_result -> next round cycle, then stop.
    # We fake only the network client; the real tool executor runs the tool.
    # ------------------------------------------------------------------
    def _native_user_env(self, provider):
        user = self.env["res.users"].create({
            "name": "Native Loop Tester %s" % provider,
            "login": "aidoo_native_%s" % provider,
            "groups_id": [(4, self.env.ref("base.group_user").id),
                          (4, self.env.ref("aidoo.group_ai_user").id)],
        })
        user_env = self.env(user=user)
        session = user_env["aidoo.session"].create(
            {"user_id": user.id, "provider": provider})
        adapter = user_env["aidoo.provider.%s" % provider]
        ctx = {
            "allowed_tools": ["orm_read"], "max_turns": 30,
            "identity": "ODOO USER CONTEXT", "is_first": True,
            "prompt": "what is my login?", "deadline": None,
        }
        return user, user_env, session, adapter, ctx

    def test_zai_native_tool_loop(self):
        from odoo.addons.aidoo.models.providers import zai_client
        user, user_env, session, adapter, ctx = self._native_user_env("zai")
        self.env["ir.config_parameter"].sudo().set_param(
            "aidoo.zai.api_key", "test-key")
        args = ('{"model": "res.users", "ids": [%s], "fields": ["login"]}'
                % user.id)
        rounds = [
            # Round 0: the model asks to call orm_read.
            [{"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_1",
                 "function": {"name": "orm_read", "arguments": args}}]}}]}],
            # Round 1: the model answers in plain text and stops (no tool calls).
            [{"choices": [{"delta": {"content": "Your login is "
                                     "aidoo_native_zai."}}]}],
        ]
        calls = {"n": 0}

        def fake_stream_chat(self, model, messages, **kw):
            i = calls["n"]
            calls["n"] += 1
            for chunk in rounds[i]:
                yield chunk

        with patch.object(zai_client.ZaiClient, "stream_chat",
                          new=fake_stream_chat), self._no_audit():
            ops = list(adapter._run_stream(user_env, session, ctx))

        kinds = [o["op"] for o in ops]
        self.assertIn("tool_use", kinds)
        self.assertIn("tool_result", kinds)
        self.assertIn("text", kinds)
        # The loop made exactly two model rounds and then terminated.
        self.assertEqual(calls["n"], 2)
        tool_use = next(o for o in ops if o["op"] == "tool_use")
        self.assertEqual(tool_use["name"], "orm_read")
        tool_result = next(o for o in ops if o["op"] == "tool_result")
        self.assertFalse(tool_result["is_error"])
        self.assertIn("aidoo_native_zai", tool_result["text"])

    def test_zai_native_loop_stops_at_round_cap(self):
        """A model that calls a tool forever is bounded by max_turns and emits the
        cap notice, never an unbounded loop."""
        from odoo.addons.aidoo.models.providers import zai_client
        user, user_env, session, adapter, ctx = self._native_user_env("zai")
        ctx["max_turns"] = 3
        self.env["ir.config_parameter"].sudo().set_param(
            "aidoo.zai.api_key", "test-key")
        args = '{"model": "res.users", "ids": [%s]}' % user.id
        calls = {"n": 0}

        def always_calls_tool(self, model, messages, **kw):
            calls["n"] += 1
            yield {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "c", "function": {
                    "name": "orm_read", "arguments": args}}]}}]}

        with patch.object(zai_client.ZaiClient, "stream_chat",
                          new=always_calls_tool), self._no_audit():
            ops = list(adapter._run_stream(user_env, session, ctx))

        self.assertEqual(calls["n"], 3)  # capped at max_turns
        self.assertTrue(any("stopped after" in o.get("text", "")
                            for o in ops if o["op"] == "text"))

    def test_gemini_native_tool_loop(self):
        from odoo.addons.aidoo.models.providers import codeassist, gemini
        user, user_env, session, adapter, ctx = self._native_user_env("gemini")
        # A configured project makes resolution authoritative and skips the
        # auto-cache filesystem write, keeping the test side-effect free.
        user.sudo().aidoo_cloud_project = "proj"
        rounds = [
            # Round 0: a functionCall part asking for orm_read.
            [{"candidates": [{"content": {"parts": [
                {"functionCall": {"name": "orm_read", "args": {
                    "model": "res.users", "ids": [user.id],
                    "fields": ["login"]}}}]}}]}],
            # Round 1: a text part and stop.
            [{"candidates": [{"content": {"parts": [
                {"text": "Your login is aidoo_native_gemini."}]}}]}],
        ]
        calls = {"n": 0}

        def fake_stream_generate(self, model, contents, **kw):
            i = calls["n"]
            calls["n"] += 1
            for resp in rounds[i]:
                yield resp

        with patch.object(gemini.AidooProviderGemini, "_load_oauth",
                          new=lambda self, u: {"access_token": "x"}), \
                patch.object(codeassist.CodeAssistClient, "ensure_project",
                             new=lambda self, meta=None: "proj"), \
                patch.object(codeassist.CodeAssistClient, "stream_generate",
                             new=fake_stream_generate), self._no_audit():
            ops = list(adapter._run_stream(user_env, session, ctx))

        kinds = [o["op"] for o in ops]
        self.assertIn("tool_use", kinds)
        self.assertIn("tool_result", kinds)
        self.assertIn("text", kinds)
        self.assertEqual(calls["n"], 2)
        self.assertIn("aidoo_native_gemini",
                      next(o for o in ops if o["op"] == "tool_result")["text"])


@tagged("post_install", "-at_install", "aidoo")
class TestClaudeStreaming(TransactionCase):

    def _run(self, events):
        adapter = self.env["aidoo.provider.claude"]
        state, ops = {}, []
        for evt in events:
            ops.extend(adapter._normalize_event(evt, state))
        return ops

    def test_deltas_then_final_replace(self):
        se = lambda e: {"type": "stream_event", "event": e}
        ops = self._run([
            se({"type": "message_start", "message": {"id": "m1"}}),
            se({"type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Hel"}}),
            se({"type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "lo"}}),
            {"type": "assistant", "message": {
                "id": "m1", "content": [{"type": "text", "text": "Hello"}]}},
        ])
        self.assertEqual([o["text"] for o in ops], ["Hel", "lo", "Hello"])
        self.assertTrue(ops[-1].get("replace"))
        self.assertFalse(ops[0].get("replace"))

    def test_non_streamed_assistant_appends(self):
        ops = self._run([{"type": "assistant", "message": {
            "id": "m2", "content": [{"type": "text", "text": "Hi"}]}}])
        self.assertEqual(ops, [{"op": "text", "msg_id": "m2", "text": "Hi"}])
