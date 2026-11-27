# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Node-local state on a multi-node cluster (several Odoo servers, one database).

Each "node" is simulated by pointing the provider home root at its own temp
dir: whatever one node stores in the database must be usable from the other."""
import json
import os
import socket
import sys
import tempfile
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "aidoo")
class TestClusterState(TransactionCase):

    def setUp(self):
        super().setUp()
        self.ICP = self.env["ir.config_parameter"].sudo()
        self.node_a = tempfile.mkdtemp(prefix="aidoo_node_a_")
        self.node_b = tempfile.mkdtemp(prefix="aidoo_node_b_")
        self.claude = self.env["aidoo.provider.claude"]
        self.user = self.env.user
        self.session = self.env["aidoo.session"].create(
            {"user_id": self.user.id, "provider": "claude"})

    def _on_node(self, root):
        """Switch to ``root`` as this node's home root (and forget which
        logins this transaction already synced, as a new request would)."""
        self.ICP.set_param("aidoo.claude.home_root", root)
        self.ICP.set_param("aidoo.antigravity.home_root", root)
        self.env.cr.__dict__.pop("_aidoo_cred_synced", None)

    def _transcript(self, root, content):
        path = os.path.join(root, str(self.user.id), ".claude", "projects",
                            "-scratch-x", self.session.cli_session_id + ".jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return path

    def _creds(self, root):
        return os.path.join(root, str(self.user.id), ".claude", ".credentials.json")

    def _argv(self, root, is_first=False):
        scratch = tempfile.mkdtemp(prefix="aidoo_scratch_")
        ctx = {
            "provider": "claude", "scratch": scratch,
            "uploads_dir": scratch + "/uploads", "python_bin": sys.executable,
            "cli_path": "/usr/bin/claude", "model": "", "max_turns": 30,
            "allowed_tools": ["orm_read"], "identity": "ODOO USER CONTEXT",
            "is_first": is_first, "prompt": "hello world",
            "session_id": self.session.id,
            "cli_session_id": self.session.cli_session_id,
            "oauth_env": {"CLAUDE_CONFIG_DIR": os.path.join(
                root, str(self.user.id), ".claude")},
            "base_url": "http://127.0.0.1:8069", "target_db": "testdb",
            "routing_sid": "sid", "token": "tok", "bridge_script": "/tmp/bridge.py",
            "excluded_models": [], "output_format": "",
        }
        return self.claude._prepare_run(ctx)["argv"]

    # ------------------------------------------------------------------
    # Conversation state
    # ------------------------------------------------------------------
    def test_transcript_follows_the_conversation(self):
        self._on_node(self.node_a)
        self._transcript(self.node_a, '{"turn": 1}\n')
        self.assertTrue(self.session._store_cli_state(self.claude, self.user))
        self.assertFalse(self.session._store_cli_state(self.claude, self.user),
                         "an unchanged transcript is not rewritten")

        self._on_node(self.node_b)
        self.assertEqual(self.session._restore_cli_state(self.claude, self.user), 1)
        restored = self.claude._session_state_files(self.session, self.user)
        with open(restored[0]) as f:
            self.assertEqual(f.read(), '{"turn": 1}\n')
        argv = self._argv(self.node_b)
        self.assertIn("--resume", argv)

    def test_missing_transcript_replays_history(self):
        self._on_node(self.node_b)
        Message = self.env["aidoo.message"]
        Message.create({"session_id": self.session.id, "role": "user",
                        "body": "Post groups A and B"})
        Message.create({"session_id": self.session.id, "role": "assistant",
                        "body": "Shall I post them?"})
        Message.create({"session_id": self.session.id, "role": "user",
                        "body": "hello world"})
        argv = self._argv(self.node_b)
        self.assertIn("--session-id", argv)
        prompt = argv[-1]
        self.assertFalse(prompt.startswith("-"))
        self.assertIn("Post groups A and B", prompt)
        self.assertIn("Shall I post them?", prompt)
        self.assertEqual(prompt.count("hello world"), 1,
                         "the message being answered is not replayed twice")

    def test_state_path_outside_home_is_refused(self):
        import base64
        import gzip
        self._on_node(self.node_b)
        evil = {"../../escape.txt": base64.b64encode(b"x").decode()}
        self.session.sudo().cli_state = base64.b64encode(
            gzip.compress(json.dumps(evil).encode()))
        self.assertEqual(self.session._restore_cli_state(self.claude, self.user), 0)
        self.assertFalse(os.path.exists(os.path.join(self.node_b, "escape.txt")))

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------
    def _login(self, token):
        return json.dumps({"claudeAiOauth": {
            "accessToken": token, "refreshToken": "r-" + token,
            "expiresAt": 9999999999999}})

    def test_login_reaches_every_node(self):
        self._on_node(self.node_a)
        self.claude._write_user_file(
            self.user, self._creds(self.node_a), self._login("a1"))

        self._on_node(self.node_b)
        self.assertTrue(self.claude._is_authenticated(self.user))
        self.assertIn("a1", open(self._creds(self.node_b)).read())

        # A refresh on B reaches A, which has not touched its copy since.
        self.claude._write_user_file(
            self.user, self._creds(self.node_b), self._login("b2"))
        self._on_node(self.node_a)
        self.claude._home_dir(self.user)
        self.assertIn("b2", open(self._creds(self.node_a)).read())

        # Logout on A is a logout everywhere (pushed by the logout itself).
        self.claude._auth_logout(self.user)
        self._on_node(self.node_b)
        self.assertFalse(self.claude._is_authenticated(self.user))
        self.assertFalse(os.path.exists(self._creds(self.node_b)))

    def test_refresh_reuses_a_refresh_done_elsewhere(self):
        expired = json.dumps({"claudeAiOauth": {
            "accessToken": "old", "refreshToken": "r-old", "expiresAt": 1000}})
        self._on_node(self.node_a)
        self.claude._write_user_file(self.user, self._creds(self.node_a), expired)
        self._on_node(self.node_b)
        self.claude._home_dir(self.user)  # B now holds the same expired login
        # A refreshes; B must pick it up instead of spending r-old again.
        self._on_node(self.node_a)
        self.claude._write_user_file(
            self.user, self._creds(self.node_a), self._login("fresh"))
        self._on_node(self.node_b)
        self.env.cr.__dict__.setdefault("_aidoo_cred_synced", set()).add(
            ("claude", self.user.id))  # B's request already synced before A's push
        with patch("requests.post") as post:
            self.assertFalse(self.claude._refresh_credentials(self.user))
            post.assert_not_called()
        self.assertIn("fresh", open(self._creds(self.node_b)).read())

    # ------------------------------------------------------------------
    # Stop / reaper
    # ------------------------------------------------------------------
    def test_pid_from_another_node_is_never_signalled(self):
        self.session.write({"state": "running", "last_run_pid": 4242,
                            "last_run_host": "some-other-node"})
        with patch("os.kill") as kill:
            self.session.action_stop()
            kill.assert_not_called()
        self.assertEqual(self.session.state, "idle")

        self.session.write({"state": "running", "last_run_pid": 4242,
                            "last_run_host": socket.gethostname()})
        with patch("os.kill") as kill:
            self.session.action_stop()
            kill.assert_called_once_with(4242, 15)

    # ------------------------------------------------------------------
    # Antigravity (native, Google login)
    # ------------------------------------------------------------------
    def _agy_creds(self, token, expiry):
        return json.dumps({"access_token": token, "refresh_token": "r-agy",
                           "expiry_date": expiry, "token_type": "Bearer"})

    def test_antigravity_login_refresh_and_logout_reach_every_node(self):
        agy = self.env["aidoo.provider.antigravity"]
        self._on_node(self.node_a)
        agy._write_user_file(self.user, agy._oauth_creds_path(self.user),
                             self._agy_creds("a1", 1000))
        agy._write_user_file(self.user, agy._project_path(self.user), "proj-1")

        self._on_node(self.node_b)
        self.assertEqual(agy._load_oauth(self.user)["access_token"], "a1")
        self.assertEqual(agy._read_text(agy._project_path(self.user)), "proj-1")

        # A refresh on A (what CodeAssistClient's save_creds does mid-turn).
        self._on_node(self.node_a)
        agy._write_user_file(self.user, agy._oauth_creds_path(self.user),
                             self._agy_creds("a2", 2000))
        self._on_node(self.node_b)
        self.assertEqual(agy._load_oauth(self.user)["access_token"], "a2")

        # Logout on B removes the login on A at A's next request.
        agy._auth_logout(self.user)
        self._on_node(self.node_a)
        self.assertFalse(agy._is_authenticated(self.user))
        self.assertFalse(os.path.exists(agy._project_path(self.user)))

    # ------------------------------------------------------------------
    # Native runs honor a Stop made on any node
    # ------------------------------------------------------------------
    def test_native_run_ends_when_the_session_is_stopped(self):
        runner = self.env["aidoo.runner"]
        session = self.session
        session.write({"state": "running"})
        events = []

        class FakeAdapter:
            def _run_stream(self, env, sess, ctx):
                try:
                    yield {"op": "text", "msg_id": "m1", "text": "partial"}
                    # The Stop, as another node would commit it.
                    sess.write({"state": "idle", "last_run_host": False})
                    yield {"op": "tool_use", "msg_id": "m1", "id": "t1",
                           "name": "orm_read", "input": {}}
                    events.append("ran past the stop")
                except GeneratorExit:
                    events.append("closed")
                    raise

        ctx = {"provider": "antigravity", "session_id": session.id,
               "timeout_s": 60, "is_first": False}
        with patch.object(type(runner), "_adapter", return_value=FakeAdapter()), \
                patch("odoo.addons.aidoo.models.aidoo_runner._STOP_CHECK_S", 0), \
                patch.object(self.env.cr, "commit", lambda: None):
            runner._execute_native(self.env, session, ctx)

        self.assertEqual(events, ["closed"])
        self.assertEqual(session.state, "idle", "the Stop is not overwritten")
        self.assertFalse(session.last_run_host)
        calls = [c for m in session.message_ids for c in (m.tool_calls or [])]
        self.assertFalse(calls, "no tool call after the Stop")
        self.assertIn("partial", "".join(session.message_ids.mapped("body")))
