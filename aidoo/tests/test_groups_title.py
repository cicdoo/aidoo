# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Model-written conversation titles (runner tag extraction), message groups
and the group/move routes."""
import json
from unittest.mock import patch

from odoo.tests.common import HttpCase, TransactionCase, tagged


@tagged("post_install", "-at_install", "aidoo")
class TestTitleTag(TransactionCase):

    def setUp(self):
        super().setUp()
        self.runner = self.env["aidoo.runner"]
        self.session = self.env["aidoo.session"].create(
            {"user_id": self.env.user.id, "provider": "claude"})

    def _run(self, ops, is_first=True):
        tstate = self.runner._title_state({"is_first": is_first})
        msg_map, produced = {}, {"any": False}
        for raw in ops:
            for op in self.runner._title_filter(self.session, tstate, raw):
                self.runner._apply_op(self.env, self.session, op, msg_map, produced)
        for op in self.runner._title_release(tstate, done=True):
            self.runner._apply_op(self.env, self.session, op, msg_map, produced)
        return "".join(self.session.message_ids.filtered(
            lambda m: m.role == "assistant").mapped("body"))

    def _text(self, *chunks):
        return [{"op": "text", "msg_id": "m1", "text": c} for c in chunks]

    def test_tag_in_one_chunk(self):
        body = self._run(self._text("<aidoo_title>Sales report</aidoo_title>\nHere it is."))
        self.assertEqual(body, "Here it is.")
        self.assertEqual(self.session.name, "Sales report")

    def test_tag_split_across_chunks(self):
        body = self._run(self._text(
            "<aid", "oo_title>Monthly ", "invoices</aidoo", "_title>\n\nHello", " there"))
        self.assertEqual(body, "Hello there")
        self.assertEqual(self.session.name, "Monthly invoices")

    def test_no_tag_keeps_default_name_and_text(self):
        default = self.session.name
        body = self._run(self._text("Hello", " world"))
        self.assertEqual(body, "Hello world")
        self.assertEqual(self.session.name, default)

    def test_partial_tag_prefix_is_released(self):
        body = self._run(self._text("<aid"))
        self.assertEqual(body, "<aid")

    def test_unclosed_tag_is_released_after_limit(self):
        long = "<aidoo_title>" + "x" * 400
        self.assertEqual(self._run(self._text(long)), long)

    def test_existing_name_is_kept_but_tag_stripped(self):
        self.session.name = "My chat"
        body = self._run(self._text("<aidoo_title>Other</aidoo_title>Answer"))
        self.assertEqual(body, "Answer")
        self.assertEqual(self.session.name, "My chat")

    def test_not_first_turn_is_untouched(self):
        text = "<aidoo_title>Other</aidoo_title>Answer"
        self.assertEqual(self._run(self._text(text), is_first=False), text)

    def test_replace_and_done_text_are_stripped(self):
        body = self._run([
            {"op": "text", "msg_id": "m1", "text": "<aidoo_title>T1</aidoo_title>A", "replace": True}])
        self.assertEqual(body, "A")
        self.assertEqual(self.session.name, "T1")
        s2 = self.env["aidoo.session"].create(
            {"user_id": self.env.user.id, "provider": "claude"})
        self.session = s2
        body = self._run([{"op": "done", "text": "<aidoo_title>T2</aidoo_title>B"}])
        self.assertEqual(body, "B")
        self.assertEqual(s2.name, "T2")

    def test_send_does_not_rename_from_user_text(self):
        captured = {}

        def _capture(self_, session, prompt, is_first, token, pending_audio=None):
            captured["prompt"] = prompt

        with patch("odoo.addons.aidoo.models.aidoo_runner.AiAssistantRunner._launch",
                   _capture), \
                patch("odoo.addons.aidoo.models.providers.claude."
                      "AidooProviderClaude._is_authenticated", return_value=True):
            self.session.send_message("What are my open invoices?")
        self.assertEqual(self.session.name, "New conversation")
        self.assertIn("<aidoo_title>", captured["prompt"])
        self.assertNotIn("<aidoo_title>", self.session.message_ids[0].body)


@tagged("post_install", "-at_install", "aidoo")
class TestMessageGroups(HttpCase):

    def setUp(self):
        super().setUp()
        self.password = "aidoo_groups_pw"
        groups = [self.env.ref("base.group_user").id,
                  self.env.ref("aidoo.group_ai_user").id]
        self.user = self.env["res.users"].create({
            "name": "Groups User", "login": "aidoo_groups_user",
            "password": self.password, "groups_id": [(6, 0, groups)]})
        self.other = self.env["res.users"].create({
            "name": "Groups Other", "login": "aidoo_groups_other",
            "password": self.password, "groups_id": [(6, 0, groups)]})
        Session = self.env["aidoo.session"]
        self.conv = Session.create({"user_id": self.user.id, "provider": "claude"})
        self.conv2 = Session.create({"user_id": self.user.id, "provider": "claude"})
        self.foreign = Session.create({"user_id": self.other.id, "provider": "claude"})
        Msg = self.env["aidoo.message"]
        self.msg = Msg.create({"session_id": self.conv.id, "role": "user", "body": "a"})
        self.msg2 = Msg.create({"session_id": self.conv2.id, "role": "user", "body": "b"})
        self.fmsg = Msg.create({"session_id": self.foreign.id, "role": "user", "body": "c"})
        self.authenticate("aidoo_groups_user", self.password)

    def _call(self, route, params):
        resp = self.url_open(route, data=json.dumps(
            {"jsonrpc": "2.0", "method": "call", "params": params}),
            headers={"Content-Type": "application/json"})
        return resp.json()

    def _create_group(self, session, name="G"):
        return self._call("/aidoo/group/create",
                          {"session_id": session.id, "name": name})["result"]

    def test_crud_and_messages_payload(self):
        g = self._create_group(self.conv, "First")
        self.assertEqual(g["name"], "First")
        upd = self._call("/aidoo/group/update", {
            "group_id": g["id"], "name": "Renamed", "collapsed": True})["result"]
        self.assertEqual((upd["name"], upd["collapsed"]), ("Renamed", True))
        data = self._call("/aidoo/messages", {"session_id": self.conv.id})["result"]
        self.assertEqual([x["id"] for x in data["groups"]], [g["id"]])
        self.assertTrue(data["groups"][0]["collapsed"])

    def test_move_keeps_create_date_and_delete_ungroups(self):
        g = self._create_group(self.conv)
        created = self.msg.create_date
        res = self._call("/aidoo/message/move",
                         {"message_id": self.msg.id, "group_id": g["id"]})["result"]
        self.assertEqual(res["group_id"], g["id"])
        self.msg.invalidate_recordset()
        self.assertEqual(self.msg.group_id.id, g["id"])
        self.assertEqual(self.msg.create_date, created)
        out = self._call("/aidoo/messages", {"session_id": self.conv.id})["result"]
        self.assertEqual(out["messages"][0]["group_id"], g["id"])
        self._call("/aidoo/group/delete", {"group_id": g["id"]})
        self.msg.invalidate_recordset()
        self.assertFalse(self.msg.group_id)
        self.assertTrue(self.msg.exists())

    def test_cross_session_move_rejected(self):
        g = self._create_group(self.conv)
        body = self._call("/aidoo/message/move",
                          {"message_id": self.msg2.id, "group_id": g["id"]})
        self.assertIn("error", body)
        self.msg2.invalidate_recordset()
        self.assertFalse(self.msg2.group_id)

    def test_cross_user_rejected(self):
        fgroup = self.env["aidoo.message.group"].create(
            {"session_id": self.foreign.id, "name": "F"})
        mine = self._create_group(self.conv)
        for route, params in (
            ("/aidoo/group/create", {"session_id": self.foreign.id, "name": "x"}),
            ("/aidoo/group/update", {"group_id": fgroup.id, "name": "x"}),
            ("/aidoo/group/delete", {"group_id": fgroup.id}),
            ("/aidoo/message/move", {"message_id": self.fmsg.id, "group_id": False}),
            ("/aidoo/message/move", {"message_id": self.msg.id, "group_id": fgroup.id}),
            ("/aidoo/message/move", {"message_id": self.fmsg.id, "group_id": mine["id"]}),
        ):
            self.assertIn("error", self._call(route, params), route)
        self.assertTrue(fgroup.exists())
        self.assertEqual(fgroup.name, "F")
