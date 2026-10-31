# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import re

from odoo import fields, models

# Server-side mirror of splitSegments() in static/src/artifacts.js: a fenced
# block is an HTML artifact when tagged ```html, or untagged with content that
# starts with a tag. Keep both in sync. Unclosed fences (mid-stream) never
# match — the non-greedy body requires the closing ```.
_FENCE_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)


def extract_html_blocks(text):
    """Return the embedded HTML block strings of a markdown body, in order."""
    blocks = []
    for m in _FENCE_RE.finditer(text or ""):
        lang = (m.group(1) or "").strip().lower()
        content = m.group(2)
        if lang == "html" or (lang == "" and re.match(r"\s*<[!a-zA-Z]", content)):
            blocks.append(content)
    return blocks


ARTIFACT_MARKER = "aidoo_artifact"


class AiAssistantMessage(models.Model):
    _name = "aidoo.message"
    _description = "Aidoo AI Assistant Message"
    _order = "create_date asc, id asc"

    session_id = fields.Many2one(
        "aidoo.session", required=True, ondelete="cascade", index=True)
    role = fields.Selection(
        [("user", "User"), ("assistant", "Assistant"),
         ("tool", "Tool"), ("report", "Report"), ("error", "Error"),
         ("divider", "Divider")],
        required=True, default="assistant")
    body = fields.Text()
    # The CLI's message id used to upsert streamed assistant text (provider-specific).
    cli_msg_id = fields.Char(index=True, copy=False)
    # List of {id, name, input, result, status} dicts for tool_use chips.
    tool_calls = fields.Json()
    # List of {id, name, mimetype} dicts for files the user attached to the turn.
    attachments = fields.Json()
    # Optional user-defined group (collapsible bucket); empty = implicit turn group.
    group_id = fields.Many2one(
        "aidoo.message.group", ondelete="set null", index=True, copy=False)

    def _artifact_blocks(self):
        """The HTML artifact sources of this message ([] when not applicable)."""
        self.ensure_one()
        if self.role == "assistant":
            return extract_html_blocks(self.body)
        if self.role == "report":
            return [self.body] if self.body else []
        return []

    def _persist_artifacts(self):
        """Persist each generated HTML block as an ir.attachment on the session.

        Runs as the invoking user (the session owner), so the attachment link
        check passes through the owner record rule. Core's ``_check_contents``
        neuters html-ish mimetypes to text/plain for non-view-editors — that is
        fine: the /aidoo/artifact route sets Content-Type itself. Idempotent:
        messages that already carry artifact descriptors are skipped. Returns
        the messages whose descriptors changed (for bus re-emission).
        """
        changed = self.env["aidoo.message"]
        Attachment = self.env["ir.attachment"]
        for msg in self:
            existing = msg.attachments or []
            if any(d.get("kind") == "artifact" for d in existing):
                continue
            blocks = msg._artifact_blocks()
            if not blocks:
                continue
            descriptors = [dict(d) for d in existing]
            for n, block in enumerate(blocks):
                att = Attachment.create({
                    "name": "artifact-%s-%s-%s.html" % (
                        (msg.session_id.scratch_uuid or "")[:8], msg.id, n + 1),
                    "raw": block.encode("utf-8"),
                    "res_model": "aidoo.session",
                    "res_id": msg.session_id.id,
                    "description": ARTIFACT_MARKER,
                })
                descriptors.append({
                    "id": att.id,
                    "name": att.name,
                    "mimetype": "text/html",
                    "kind": "artifact",
                    "index": n,
                    "url": "/aidoo/artifact/%d" % att.id,
                })
            msg.attachments = descriptors
            changed |= msg
        return changed

    def _to_frontend(self):
        """Serialize for the OWL chat (matches the bus payload shape)."""
        return [{
            "id": m.id,
            "role": m.role,
            "body": m.body or "",
            "tool_calls": m.tool_calls or [],
            "attachments": m.attachments or [],
            "create_date": fields.Datetime.to_string(m.create_date),
            "group_id": m.group_id.id or False,
        } for m in self]
