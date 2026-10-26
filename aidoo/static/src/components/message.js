/** @odoo-module **/
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import { Component, proxy, t, useProps } from "@odoo/owl";
import { renderMarkdown } from "../markdown";
import { splitSegments } from "../artifacts";

export class AiMessage extends Component {
    static template = "aidoo.Message";
    props = useProps({
        message: t.object(),
        // Called with an HTML string to display it in the artifact panel.
        onOpenArtifact: t.function().optional(),
    });

    setup() {
        // Tool calls are collapsed by default; this toggles the per-message view.
        this.ui = proxy({ toolsOpen: false });
    }

    get roleLabel() {
        return { user: "You", assistant: "Assistant", report: "Report", error: "Error" }[
            this.props.message.role
        ] || this.props.message.role;
    }

    get isReport() {
        return this.props.message.role === "report";
    }

    // A run boundary inserted between scheduled-agent runs: rendered as a
    // labelled horizontal rule rather than a message bubble.
    get isDivider() {
        return this.props.message.role === "divider";
    }

    // Server-persisted artifact descriptors for this message, ordered by their
    // html-block index. Empty for historical/streaming messages (srcdoc fallback).
    get artifactDescriptors() {
        return (this.props.message.attachments || [])
            .filter((a) => a.kind === "artifact")
            .sort((a, b) => (a.index || 0) - (b.index || 0));
    }

    // Ordered render segments for non-user messages: markdown chunks (rendered
    // to safe HTML) interleaved with embedded HTML chunks shown as a card that
    // opens the artifact panel. A report-role message is a single HTML segment.
    // The nth HTML segment carries the nth artifact descriptor (when the run
    // has finished and the server persisted it). User messages stay plain text
    // and don't use this.
    get segments() {
        const body = this.props.message.body || "";
        const artifacts = this.artifactDescriptors;
        if (this.isReport) {
            return [{ type: "html", html: body, artifact: artifacts[0] }];
        }
        const out = [];
        let n = 0;
        for (const seg of splitSegments(body)) {
            if (!seg.text.trim()) {
                continue;
            }
            out.push(seg.type === "html"
                ? { type: "html", html: seg.text, artifact: artifacts[n++] }
                : { type: "md", html: renderMarkdown(seg.text) });
        }
        return out;
    }

    openArtifact(seg) {
        this.props.onOpenArtifact?.(seg);
    }

    get toolCalls() {
        return this.props.message.tool_calls || [];
    }

    toggleTools() {
        this.ui.toolsOpen = !this.ui.toolsOpen;
    }

    // True while at least one tool call is still running.
    get toolsBusy() {
        return this.toolCalls.some(
            (c) => c.status !== "done" && c.status !== "error");
    }

    // Upload chips only — generated artifacts render as cards, not chips.
    get attachments() {
        return (this.props.message.attachments || [])
            .filter((a) => a.kind !== "artifact");
    }

    attachmentIcon(att) {
        const mt = att.mimetype || "";
        if (mt.startsWith("image/")) return "image";
        if (mt === "application/pdf") return "picture_as_pdf";
        return "description";
    }

    statusIcon(call) {
        if (call.status === "done") return "check";
        if (call.status === "error") return "close";
        return "autorenew";
    }

    summarizeInput(call) {
        try {
            const s = JSON.stringify(call.input || {});
            return s.length > 140 ? s.slice(0, 140) + "…" : s;
        } catch {
            return "";
        }
    }
}
