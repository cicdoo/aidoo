/** @odoo-module **/
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import {
    Component, useRef, onPatched, onWillPatch, onMounted, onWillUnmount,
} from "@odoo/owl";
import { AiMessage } from "./message";

// Trigger an older-page load once the user scrolls within this many px of the top.
const LOAD_MORE_THRESHOLD = 80;
// Treat the view as "pinned to bottom" within this many px of the end.
const STICK_BOTTOM_THRESHOLD = 40;

export class AiMessageList extends Component {
    static template = "aidoo.MessageList";
    static components = { AiMessage };
    static props = {
        messages: Array,
        running: Boolean,
        hasMore: { type: Boolean, optional: true },
        onLoadMore: { type: Function, optional: true },
        onOpenArtifact: { type: Function, optional: true },
    };

    setup() {
        this.scrollRef = useRef("scroll");
        this._stickBottom = true;    // keep pinned to the latest message
        this._loadingMore = false;   // a page fetch is in flight
        this._restoreScroll = false; // preserve viewport after prepending older
        this._onScroll = this._onScroll.bind(this);
        onMounted(() => {
            this._scrollToBottom();
            this.scrollRef.el?.addEventListener("scroll", this._onScroll, {
                passive: true,
            });
        });
        onWillUnmount(() => {
            this.scrollRef.el?.removeEventListener("scroll", this._onScroll);
        });
        // Capture pre-patch geometry so we can keep the viewport stable when
        // older messages are prepended (otherwise the content jumps).
        onWillPatch(() => {
            const el = this.scrollRef.el;
            if (el) {
                this._prevHeight = el.scrollHeight;
                this._prevTop = el.scrollTop;
            }
        });
        onPatched(() => this._afterPatch());
    }

    _onScroll() {
        const el = this.scrollRef.el;
        if (!el) {
            return;
        }
        const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
        this._stickBottom = distFromBottom < STICK_BOTTOM_THRESHOLD;
        if (el.scrollTop < LOAD_MORE_THRESHOLD) {
            this.loadMore();
        }
    }

    // Ask the parent for an older page (from a scroll-up or the button). Guards
    // against concurrent fetches and flags the next patch as a prepend so the
    // viewport is preserved instead of jumping to the bottom.
    loadMore() {
        if (this._loadingMore || !this.props.hasMore || !this.props.onLoadMore) {
            return;
        }
        this._loadingMore = true;
        this._restoreScroll = true;
        Promise.resolve(this.props.onLoadMore()).finally(() => {
            this._loadingMore = false;
        });
    }

    _afterPatch() {
        const el = this.scrollRef.el;
        if (!el) {
            return;
        }
        if (this._restoreScroll) {
            // Older messages were prepended: offset scrollTop by the height the
            // new content added so the user's view stays put.
            el.scrollTop = el.scrollHeight - this._prevHeight + this._prevTop;
            this._restoreScroll = false;
        } else if (this._stickBottom) {
            this._scrollToBottom();
        }
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
