# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Generated HTML artifacts: extraction, persistence, and the serving routes."""
import json

from odoo.tests.common import HttpCase, TransactionCase, tagged

from ..models.aidoo_message import ARTIFACT_MARKER, extract_html_blocks

CHART = "<div style='background:#714b67'>chart</div>"


@tagged("post_install", "-at_install", "aidoo")
class TestArtifactExtraction(TransactionCase):

    def test_tagged_html_fence(self):
        self.assertEqual(
            extract_html_blocks("hi\n```html\n%s\n```\nbye" % CHART),
            ["%s\n" % CHART])

    def test_untagged_fence_with_tag_content(self):
        self.assertEqual(
            extract_html_blocks("```\n<!DOCTYPE html><p>x</p>\n```"),
            ["<!DOCTYPE html><p>x</p>\n"])
        self.assertEqual(
            extract_html_blocks("```\n  <div>x</div>\n```"),
            ["  <div>x</div>\n"])

    def test_non_html_fences_ignored(self):
        self.assertEqual(extract_html_blocks("```json\n{\"a\": 1}\n```"), [])
        self.assertEqual(extract_html_blocks("```python\nprint(1)\n```"), [])
        # Untagged fence that does not start with a tag stays markdown.
        self.assertEqual(extract_html_blocks("```\nplain text\n```"), [])

    def test_unclosed_fence_ignored(self):
        self.assertEqual(extract_html_blocks("```html\n<div>streaming…"), [])

    def test_multiple_blocks_ordered(self):
        blocks = extract_html_blocks(
            "```html\n<p>one</p>\n```\ntext\n```html\n<p>two</p>\n```")
        self.assertEqual(blocks, ["<p>one</p>\n", "<p>two</p>\n"])

    def test_empty_and_none(self):
        self.assertEqual(extract_html_blocks(""), [])
        self.assertEqual(extract_html_blocks(None), [])


@tagged("post_install", "-at_install", "aidoo")
class TestArtifactPersistence(TransactionCase):

    def setUp(self):
        super().setUp()
        self.user = self.env["res.users"].create({
            "name": "Artifact Owner",
            "login": "aidoo_artifact_owner",
            "groups_id": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("aidoo.group_ai_user").id,
            ])],
        })
        self.session = self.env["aidoo.session"].create(
            {"user_id": self.user.id, "provider": "claude"})
        self.Message = self.env["aidoo.message"].with_user(self.user)

    def _msg(self, body, role="assistant", attachments=False):
        return self.Message.create({
            "session_id": self.session.id, "role": role, "body": body,
            "attachments": attachments})

    def test_persist_creates_marked_attachments(self):
        msg = self._msg("intro\n```html\n%s\n```" % CHART)
        changed = msg._persist_artifacts()
        self.assertEqual(changed, msg)
        arts = [d for d in msg.attachments if d.get("kind") == "artifact"]
        self.assertEqual(len(arts), 1)
        att = self.env["ir.attachment"].browse(arts[0]["id"])
        self.assertEqual(att.res_model, "aidoo.session")
        self.assertEqual(att.res_id, self.session.id)
        self.assertEqual(att.description, ARTIFACT_MARKER)
        self.assertIn(CHART, att.raw.decode())
        self.assertTrue(att.name.startswith("artifact-"))
        self.assertTrue(att.name.endswith(".html"))
        self.assertEqual(arts[0]["url"], "/aidoo/artifact/%d" % att.id)
        # Body untouched (provider resume context).
        self.assertIn("```html", msg.body)

    def test_persist_appends_after_upload_descriptors(self):
        upload = {"id": 999, "name": "input.csv", "mimetype": "text/csv"}
        msg = self._msg("```html\n<p>x</p>\n```", attachments=[upload])
        msg._persist_artifacts()
        self.assertEqual(msg.attachments[0], upload)
        self.assertEqual(msg.attachments[1]["kind"], "artifact")

    def test_persist_idempotent(self):
        msg = self._msg("```html\n<p>x</p>\n```")
        msg._persist_artifacts()
        count = self.env["ir.attachment"].search_count(
            [("res_model", "=", "aidoo.session"),
             ("res_id", "=", self.session.id)])
        changed = msg._persist_artifacts()
        self.assertFalse(changed)
        self.assertEqual(count, self.env["ir.attachment"].search_count(
            [("res_model", "=", "aidoo.session"),
             ("res_id", "=", self.session.id)]))

    def test_report_role_whole_body(self):
        msg = self._msg("<h1>Report</h1>", role="report")
        msg._persist_artifacts()
        arts = [d for d in msg.attachments if d.get("kind") == "artifact"]
        self.assertEqual(len(arts), 1)
        att = self.env["ir.attachment"].browse(arts[0]["id"])
        self.assertEqual(att.raw.decode(), "<h1>Report</h1>")

    def test_other_roles_skipped(self):
        for role in ("user", "tool", "error"):
            msg = self._msg("```html\n<p>x</p>\n```", role=role)
            self.assertFalse(msg._persist_artifacts())

    def test_discuss_link_lines(self):
        msg = self._msg("answer\n```html\n<p>x</p>\n```")
        msg._persist_artifacts()
        lines = self.session._artifact_link_lines()
        self.assertIn("View: [artifact-", lines)
        self.assertIn("/aidoo/artifact/", lines)
        base = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
        self.assertIn("(%s/aidoo/artifact/" % base, lines)


@tagged("post_install", "-at_install", "aidoo")
class TestArtifactRoutes(HttpCase):

    def setUp(self):
        super().setUp()
        self.password = "aidoo_artifact_pw"
        group_ids = [(6, 0, [
            self.env.ref("base.group_user").id,
            self.env.ref("aidoo.group_ai_user").id,
        ])]
        self.owner = self.env["res.users"].create({
            "name": "Artifact Owner", "login": "aidoo_art_owner",
            "password": self.password, "groups_id": group_ids})
        self.stranger = self.env["res.users"].create({
            "name": "Artifact Stranger", "login": "aidoo_art_stranger",
            "password": self.password, "groups_id": group_ids})
        self.conv = self.env["aidoo.session"].create(
            {"user_id": self.owner.id, "provider": "claude"})
        msg = self.env["aidoo.message"].with_user(self.owner).create({
            "session_id": self.conv.id, "role": "assistant",
            "body": "```html\n%s\n```" % CHART})
        msg._persist_artifacts()
        self.att_id = [d for d in msg.attachments
                       if d.get("kind") == "artifact"][0]["id"]
        self.url = "/aidoo/artifact/%d" % self.att_id

    def _call(self, route, params):
        payload = {"jsonrpc": "2.0", "method": "call", "params": params}
        resp = self.url_open(
            route, data=json.dumps(payload),
            headers={"Content-Type": "application/json"})
        return resp.json()

    def test_owner_can_view(self):
        self.authenticate("aidoo_art_owner", self.password)
        resp = self.url_open(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers["Content-Type"])
        csp = resp.headers.get("Content-Security-Policy", "")
        self.assertIn("sandbox", csp)
        self.assertNotIn("script-src", csp)
        self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertIn(CHART, resp.text)

    def test_stranger_and_anonymous_rejected(self):
        self.authenticate("aidoo_art_stranger", self.password)
        self.assertEqual(self.url_open(self.url).status_code, 404)
        self.authenticate(None, None)
        self.assertEqual(self.url_open(self.url).status_code, 404)

    def test_share_token_flow(self):
        self.authenticate("aidoo_art_owner", self.password)
        body = self._call("/aidoo/artifact/share",
                          {"attachment_id": self.att_id})
        share_url = body["result"]["url"]
        self.assertIn("token=", share_url)

        # Anyone with the link (logged out) can view.
        self.authenticate(None, None)
        resp = self.url_open(share_url)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(CHART, resp.text)

        # Wrong token fails.
        self.assertEqual(
            self.url_open(self.url + "?token=wrong").status_code, 404)

        # Revoke kills the link.
        self.authenticate("aidoo_art_owner", self.password)
        self._call("/aidoo/artifact/revoke", {"attachment_id": self.att_id})
        self.authenticate(None, None)
        self.assertEqual(self.url_open(share_url).status_code, 404)

    def test_share_and_revoke_are_owner_only(self):
        self.authenticate("aidoo_art_stranger", self.password)
        body = self._call("/aidoo/artifact/share",
                          {"attachment_id": self.att_id})
        self.assertIn("error", body)
        body = self._call("/aidoo/artifact/revoke",
                          {"attachment_id": self.att_id})
        self.assertIn("error", body)

    def test_non_artifact_attachment_never_served(self):
        """The route must not become a generic tokenized attachment reader."""
        upload = self.env["ir.attachment"].create({
            "name": "upload.html", "raw": b"<p>secret</p>",
            "res_model": "aidoo.session", "res_id": self.conv.id,
        })
        token = upload.generate_access_token()[0]
        url = "/aidoo/artifact/%d?token=%s" % (upload.id, token)
        self.assertEqual(self.url_open(url).status_code, 404)
        self.authenticate("aidoo_art_owner", self.password)
        self.assertEqual(
            self.url_open("/aidoo/artifact/%d" % upload.id).status_code, 404)

    def test_channel_member_can_view(self):
        member_partner = self.stranger.partner_id
        channel = self.env["discuss.channel"].sudo().create({
            "name": "artifact test channel",
            "channel_type": "channel",
            "channel_partner_ids": [
                (4, self.owner.partner_id.id), (4, member_partner.id)],
        })
        self.conv.sudo().write({"discuss_channel_id": channel.id})
        self.authenticate("aidoo_art_stranger", self.password)
        self.assertEqual(self.url_open(self.url).status_code, 200)
