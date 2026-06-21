odoo.define('aidoo.message', function (require) {
"use strict";
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).

// Per-message rendering helpers, shared by the message list (chat action and the
// floating panel). In Odoo 13's static QWeb these are mixed onto the rendering
// widget so the ``aidoo.Message`` template can call e.g. widget.roleLabel(msg).
// Tool-fold open/closed state lives on the host widget (this._toolsOpen, a Set of
// message ids) since there is no per-component reactive state.
var markdown = require('aidoo.markdown');
var artifacts = require('aidoo.artifacts');

var MessageMixin = {
    roleLabel: function (msg) {
        return { user: "You", assistant: "Assistant", report: "Report", error: "Error" }[msg.role]
            || msg.role;
    },

    isReport: function (msg) {
        return msg.role === "report";
    },

    // Ordered render segments for non-user messages: markdown chunks (rendered to
    // safe HTML) interleaved with embedded HTML chunks shown as a card that opens
    // the artifact panel. A report-role message is a single HTML segment.
    segments: function (msg) {
        var body = msg.body || "";
        if (this.isReport(msg)) {
            return [{ type: "html", html: body }];
        }
        var out = [];
        artifacts.splitSegments(body).forEach(function (seg) {
            if (!seg.text.trim()) {
                return;
            }
            out.push(seg.type === "html"
                ? { type: "html", html: seg.text }
                : { type: "md", html: markdown.renderMarkdown(seg.text) });
        });
        return out;
    },

    toolCalls: function (msg) {
        return msg.tool_calls || [];
    },

    // True while at least one tool call is still running.
    toolsBusy: function (msg) {
        return this.toolCalls(msg).some(function (c) {
            return c.status !== "done" && c.status !== "error";
        });
    },

    isToolsOpen: function (msg) {
        return this._toolsOpen && this._toolsOpen.has(msg.id);
    },

    attachments: function (msg) {
        return msg.attachments || [];
    },

    attachmentIcon: function (att) {
        var mt = att.mimetype || "";
        if (mt.indexOf("image/") === 0) return "fa-file-image-o";
        if (mt === "application/pdf") return "fa-file-pdf-o";
        return "fa-file-text-o";
    },

    statusIcon: function (call) {
        if (call.status === "done") return "fa-check text-success";
        if (call.status === "error") return "fa-times text-danger";
        return "fa-spinner fa-spin text-muted";
    },

    summarizeInput: function (call) {
        try {
            var s = JSON.stringify(call.input || {});
            return s.length > 140 ? s.slice(0, 140) + "…" : s;
        } catch (e) {
            return "";
        }
    },
};

return MessageMixin;
});
