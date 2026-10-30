/** @odoo-module **/
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import {
    Component, signal, proxy, t, useProps, onPatched, onWillPatch, onMounted, onWillUnmount,
} from "@odoo/owl";
import { AiMessage } from "./message";

// Trigger an older-page load once the user scrolls within this many px of the top.
const LOAD_MORE_THRESHOLD = 80;
// Treat the view as "pinned to bottom" within this many px of the end.
const STICK_BOTTOM_THRESHOLD = 40;

export class AiMessageList extends Component {
    static template = "aidoo.MessageList";
    static components = { AiMessage };
    props = useProps({
        messages: t.array(),
        running: t.boolean(),
        hasMore: t.boolean().optional(),
        onLoadMore: t.function().optional(),
        onOpenArtifact: t.function().optional(),
        // Message groups. Without onMoveMessage (e.g. the floating companion)
        // the list renders flat, with no group headers or drag and drop.
        groups: t.array().optional(),
        onMoveMessage: t.function().optional(),
        onToggleGroup: t.function().optional(),
        onRenameGroup: t.function().optional(),
        onCreateGroup: t.function().optional(),
        onDeleteGroup: t.function().optional(),
    });

    scrollRef = signal.ref();
    renameRef = signal.ref();

    setup() {
        this.ui = proxy({
            collapsedTurns: {},   // implicit turn groups collapsed locally
            editing: null,        // key of the group header being renamed
            dragging: false,
            dragOver: null,       // key of the drop target under the cursor
        });
        this._drag = null;        // ids of the message(s) being dragged
        this._stickBottom = true;    // keep pinned to the latest message
        this._loadingMore = false;   // a page fetch is in flight
        this._restoreScroll = false; // preserve viewport after prepending older
        this._onScroll = this._onScroll.bind(this);
        onMounted(() => {
            this._scrollToBottom();
            this.scrollRef()?.addEventListener("scroll", this._onScroll, {
                passive: true,
            });
        });
        onWillUnmount(() => {
            this.scrollRef()?.removeEventListener("scroll", this._onScroll);
        });
        // Capture pre-patch geometry so we can keep the viewport stable when
        // older messages are prepended (otherwise the content jumps).
        onWillPatch(() => {
            const el = this.scrollRef();
            if (el) {
                this._prevHeight = el.scrollHeight;
                this._prevTop = el.scrollTop;
            }
        });
        onPatched(() => {
            if (this.ui.editing && this.renameRef() && !this._renameFocused) {
                this._renameFocused = true;
                this.renameRef().focus();
                this.renameRef().select();
            }
            this._afterPatch();
        });
    }

    _onScroll() {
        const el = this.scrollRef();
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
        const el = this.scrollRef();
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
                last._ids = [...last._ids, msg.id];
            } else {
                // Shallow clone so merging never mutates the upstream state.
                out.push({ ...msg, _ids: [msg.id] });
            }
        }
        return out;
    }

    get groupsEnabled() {
        return !!this.props.onMoveMessage;
    }

    // The merged messages split into collapsible buckets. A message with a
    // real group_id belongs to that group; the rest form implicit "turn"
    // groups: a user message plus the replies that follow it. A grouped user
    // message keeps pulling its ungrouped replies into its group.
    get groupedMessages() {
        const merged = this.displayMessages;
        if (!this.groupsEnabled) {
            return [{ key: "all", header: false, open: true, messages: merged }];
        }
        const groups = new Map((this.props.groups || []).map((g) => [g.id, g]));
        const buckets = [];
        const byKey = new Map();
        const make = (key, init) => {
            const b = { key, header: true, messages: [], ...init };
            byKey.set(key, b);
            buckets.push(b);
            return b;
        };
        const realBucket = (g) =>
            byKey.get(`g${g.id}`) ||
            make(`g${g.id}`, { id: g.id, name: g.name, collapsed: g.collapsed });
        let current = null;
        for (const msg of merged) {
            const g = groups.get(msg.group_id);
            if (g) {
                const b = realBucket(g);
                b.messages.push(msg);
                if (msg.role === "user") {
                    current = b;
                }
                continue;
            }
            if (msg.role === "user" || !current) {
                current = make(`t${msg.id}`, {
                    id: null,
                    name: this._turnTitle(msg),
                    collapsed: !!this.ui.collapsedTurns[`t${msg.id}`],
                });
            }
            current.messages.push(msg);
        }
        for (const g of groups.values()) {
            realBucket(g); // empty groups stay visible as drop targets
        }
        buckets.forEach((b, i) => {
            // The bucket receiving the live reply stays open while it streams.
            b.open = !b.collapsed || (this.props.running && i === buckets.length - 1);
        });
        return buckets;
    }

    _turnTitle(msg) {
        if (msg.role !== "user") {
            return "Earlier messages";
        }
        const text = (msg.body || "").replace(/\s+/g, " ").trim();
        if (!text) {
            return "Attachments";
        }
        return text.length > 60 ? text.slice(0, 60) + "…" : text;
    }

    toggleBucket(b) {
        if (b.id) {
            this.props.onToggleGroup?.(b.id);
        } else {
            this.ui.collapsedTurns[b.key] = !this.ui.collapsedTurns[b.key];
        }
    }

    startRename(b) {
        if (b.id) {
            this._renameFocused = false;
            this.ui.editing = b.key;
        }
    }

    finishRename(b, ev, commit = true) {
        if (this.ui.editing !== b.key) {
            return;
        }
        this.ui.editing = null;
        if (commit) {
            this.props.onRenameGroup?.(b.id, ev.target.value);
        }
    }

    onRenameKey(b, ev) {
        if (ev.key === "Enter") {
            this.finishRename(b, ev);
        } else if (ev.key === "Escape") {
            this.finishRename(b, ev, false);
        }
    }

    addGroup() {
        this.props.onCreateGroup?.("");
    }

    // ---- Drag and drop (native HTML5) ----
    isDraggable(msg) {
        return this.groupsEnabled && typeof msg.id === "number" && msg.role !== "divider";
    }

    onDragStart(ev, msg) {
        this._drag = msg._ids.filter((id) => typeof id === "number");
        ev.dataTransfer.effectAllowed = "move";
        ev.dataTransfer.setData("text/plain", String(msg.id));
        this.ui.dragging = true;
    }

    onDragEnd() {
        this._drag = null;
        this.ui.dragging = false;
        this.ui.dragOver = null;
    }

    onDragOver(ev, key) {
        if (!this._drag) {
            return;
        }
        ev.preventDefault();
        ev.dataTransfer.dropEffect = "move";
        this.ui.dragOver = key;
    }

    onDragLeave(ev, key) {
        if (!ev.currentTarget.contains(ev.relatedTarget) && this.ui.dragOver === key) {
            this.ui.dragOver = null;
        }
    }

    // Drop on a real group moves the message into it; on an implicit turn it
    // promotes that turn to a real group (named after it) holding the turn's
    // messages plus the dropped one. ``bucket`` is null for the ungroup zone.
    onDrop(ev, bucket) {
        ev.preventDefault();
        const ids = this._drag;
        this.onDragEnd();
        if (!ids || !ids.length) {
            return;
        }
        if (!bucket) {
            this.props.onMoveMessage(ids, false);
        } else if (bucket.id) {
            this.props.onMoveMessage(ids, bucket.id);
        } else {
            const own = bucket.messages.flatMap((m) => m._ids);
            const all = [...new Set([...own, ...ids])].filter((i) => typeof i === "number");
            this.props.onCreateGroup?.(bucket.name, all);
        }
    }

    _scrollToBottom() {
        const el = this.scrollRef();
        if (el) {
            el.scrollTop = el.scrollHeight;
        }
    }
}
