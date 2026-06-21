/** @odoo-module **/
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import { Component, useRef, onPatched, onMounted } from "@odoo/owl";
import { AiMessage } from "./message";

export class AiMessageList extends Component {
    static template = "aidoo.MessageList";
    static components = { AiMessage };
    static props = {
        messages: Array,
        running: Boolean,
        onOpenArtifact: { type: Function, optional: true },
    };

    setup() {
        this.scrollRef = useRef("scroll");
        onMounted(() => this._scrollToBottom());
        onPatched(() => this._scrollToBottom());
    }

    // A single turn often arrives as several assistant messages (Claude tags
    // each with its own id), many of which carry only tool calls and no text.
    // Merge consecutive assistant messages into one bubble so their tool calls
    // share a single collapsible fold instead of stacking up separately.
    get displayMessages() {
        const out = [];
        for (const msg of this.props.messages) {
            const last = out[out.length - 1];
            if (msg.role === "assistant" && last && last.role === "assistant") {
                const bodies = [last.body, msg.body].filter((b) => b && b.trim());
                last.body = bodies.join("\n\n");
                last.tool_calls = [...(last.tool_calls || []), ...(msg.tool_calls || [])];
                last.attachments = [...(last.attachments || []), ...(msg.attachments || [])];
            } else {
                // Shallow clone so merging never mutates the upstream state.
                out.push({ ...msg });
            }
        }
        return out;
    }

    _scrollToBottom() {
        const el = this.scrollRef.el;
        if (el) {
            el.scrollTop = el.scrollHeight;
        }
    }
}
