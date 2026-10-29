# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Sidebar conversation folders and the folder/move routes."""
import json

from odoo.tests.common import HttpCase, tagged


@tagged("post_install", "-at_install", "aidoo")
class TestSessionFolders(HttpCase):

    def setUp(self):
        super().setUp()
        self.password = "aidoo_folders_pw"
        groups = [self.env.ref("base.group_user").id,
                  self.env.ref("aidoo.group_ai_user").id]
        Users = self.env["res.users"]
        self.user = Users.create({
            "name": "Folders User", "login": "aidoo_folders_user",
            "password": self.password, "groups_id": [(6, 0, groups)]})
        self.other = Users.create({
            "name": "Folders Other", "login": "aidoo_folders_other",
            "password": self.password, "groups_id": [(6, 0, groups)]})
        Session = self.env["aidoo.session"]
        self.conv = Session.create({"user_id": self.user.id, "provider": "claude"})
        self.foreign = Session.create({"user_id": self.other.id, "provider": "claude"})
        self.ffolder = self.env["aidoo.session.folder"].create(
            {"name": "Theirs", "user_id": self.other.id})
        self.authenticate("aidoo_folders_user", self.password)

    def _call(self, route, params=None):
        resp = self.url_open(route, data=json.dumps(
            {"jsonrpc": "2.0", "method": "call", "params": params or {}}),
            headers={"Content-Type": "application/json"})
        return resp.json()

    def _create(self, name="F"):
        return self._call("/aidoo/folder/create", {"name": name})["result"]

    def test_create_update_delete(self):
        f = self._create("Work")
        self.assertEqual(f["name"], "Work")
        upd = self._call("/aidoo/folder/update", {
            "folder_id": f["id"], "name": "Renamed", "collapsed": True})["result"]
        self.assertEqual((upd["name"], upd["collapsed"]), ("Renamed", True))
        self.assertEqual(
            [x["id"] for x in self._call("/aidoo/folders")["result"]], [f["id"]])
        self._call("/aidoo/folder/delete", {"folder_id": f["id"]})
        self.assertFalse(self.env["aidoo.session.folder"].browse(f["id"]).exists())

    def test_move_in_out_and_delete_keeps_session(self):
        f = self._create()
        res = self._call("/aidoo/session/move",
                         {"session_id": self.conv.id, "folder_id": f["id"]})["result"]
        self.assertEqual(res["folder_id"], f["id"])
        self.conv.invalidate_recordset()
        self.assertEqual(self.conv.folder_id.id, f["id"])
        self._call("/aidoo/session/move", {"session_id": self.conv.id, "folder_id": False})
        self.conv.invalidate_recordset()
        self.assertFalse(self.conv.folder_id)
        self._call("/aidoo/session/move", {"session_id": self.conv.id, "folder_id": f["id"]})
        self._call("/aidoo/folder/delete", {"folder_id": f["id"]})
        self.conv.invalidate_recordset()
        self.assertTrue(self.conv.exists())
        self.assertFalse(self.conv.folder_id)

    def test_cross_user_rejected(self):
        mine = self._create()
        for route, params in (
            ("/aidoo/folder/update", {"folder_id": self.ffolder.id, "name": "x"}),
            ("/aidoo/folder/delete", {"folder_id": self.ffolder.id}),
            ("/aidoo/session/move", {"session_id": self.conv.id, "folder_id": self.ffolder.id}),
            ("/aidoo/session/move", {"session_id": self.foreign.id, "folder_id": mine["id"]}),
        ):
            self.assertIn("error", self._call(route, params), route)
        self.assertTrue(self.ffolder.exists())
        self.assertEqual(self.ffolder.name, "Theirs")
        self.foreign.invalidate_recordset()
        self.assertFalse(self.foreign.folder_id)

    def test_folders_listing_is_private(self):
        mine = self._create()
        ids = [x["id"] for x in self._call("/aidoo/folders")["result"]]
        self.assertEqual(ids, [mine["id"]])

    def test_sessions_payload(self):
        f = self._create()
        self._call("/aidoo/session/move", {"session_id": self.conv.id, "folder_id": f["id"]})
        self.env["aidoo.message"].create(
            {"session_id": self.conv.id, "role": "user", "body": "hi"})
        items = self._call("/aidoo/sessions")["result"]
        item = next(i for i in items if i["id"] == self.conv.id)
        self.assertEqual(item["folder_id"], f["id"])
        self.assertTrue(item["write_date"])
