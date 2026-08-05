/** @odoo-module **/
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import { Component, useState, onWillStart, onWillDestroy } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { rpc } from "@web/core/network/rpc";
import { user } from "@web/core/user";
import { _t } from "@web/core/l10n/translation";
import { AiMessageList } from "./components/message_list";
import { AiComposer } from "./components/composer";

// Floating "Ask Aidoo" companion shown on form and list views. The button
// (bottom-right) toggles a chat panel that floats over the current view — the
// record/list stays visible behind it. The panel can be expanded to a larger
// floating window, minimized back to the button, or closed (ends the chat).
// Seeded with the current view's context: the record on a form, or the filtered
// record set on a list. Mounted globally via the main_components registry.
export class FloatingAidoo extends Component {
    static template = "aidoo.FloatingButton";
    static components = { AiMessageList, AiComposer };
    static props = {};

    setup() {
        this.action = useService("action");
        this.aiBus = useService("aidoo");
        // Reactive: re-renders when the current view changes.
        this.view = useState(useService("aidoo_view_context").state);
        this.state = useState({
            open: false,
            expanded: false,
            ready: false,
            contextLabel: "",
            sessionId: null,
            messages: [],
            running: false,
            authError: "",
            skills: [],
            skillIds: [],
        });
        this._allowed = false;

        onWillDestroy(() => {
            if (this.state.sessionId) {
                this.aiBus.unregister(this.state.sessionId);
                this._discardIfEmpty(this.state.sessionId);
            }
        });
        onWillStart(async () => {
            this._allowed = await user.hasGroup("aidoo.group_ai_user");
            if (this._allowed) {
                try {
                    const info = await rpc("/aidoo/providers");
                    this.state.skills = info.skills || [];
                } catch {
                    // Non-fatal: picker just shows empty; FAB still works.
                }
            }
        });
    }

    // ------------------------------------------------------------------
    // Visibility (driven by the current data view)
    // ------------------------------------------------------------------
    get showFab() {
        return this._allowed && !!this.view.current;
    }

    get panelOpen() {
        return this.showFab && this.state.open;
    }

    // Context payload describing what the user is looking at.
    _payload() {
        const c = this.view.current;
        if (!c) {
            return {};
        }
        const p = { context_model: c.resModel };
        if (c.viewType === "form" && c.resId) {
            p.context_res_id = c.resId;
            return p;
        }
        // Any multi-record view: forward the active filters/grouping/domain and
        // the user's record selection.
        p.context_view_type = c.viewType;
        if (c.domain && c.domain.length) {
            p.context_domain = JSON.stringify(c.domain);
        }
        if (c.groupby && c.groupby.length) {
            p.context_groupby = JSON.stringify(c.groupby);
        }
        if (c.filters) {
            p.context_filters = c.filters;
        }
        // Explicit selection only — when "select all" is on, the domain already
        // represents the set, so we don't ship thousands of ids.
        if (c.selectedIds && c.selectedIds.length && !c.selectAll) {
            p.context_selected_ids = JSON.stringify(c.selectedIds.slice(0, 200));
        }
        return p;
    }

    // ------------------------------------------------------------------
    // Panel controls
    // ------------------------------------------------------------------
    async toggle() {
        this.state.open = !this.state.open;
        if (this.state.open && !this.state.sessionId) {
            await this._startSession();
        }
    }

    minimize() {
        // Collapse to the button, keep the conversation.
        this.state.open = false;
    }

    toggleExpand() {
        this.state.expanded = !this.state.expanded;
    }

    close() {
        // Collapse and end the conversation (next open starts fresh).
        this.state.open = false;
        this.state.expanded = false;
        if (this.state.sessionId) {
            this.aiBus.unregister(this.state.sessionId);
            // Drop the conversation if it was never used (no messages sent).
            this._discardIfEmpty(this.state.sessionId);
        }
        this.state.sessionId = null;
        this.state.messages = [];
        this.state.skillIds = [];
        this.state.contextLabel = "";
    }

    // ------------------------------------------------------------------
    // Session lifecycle
    // ------------------------------------------------------------------
    async _startSession() {
        this.state.ready = false;
        this.state.authError = "";
        const s = await rpc("/aidoo/new", this._payload());
        this._bind(s.id);
        this.state.contextLabel = s.context_label || "";
        const data = await rpc("/aidoo/messages", { session_id: s.id });
        this.state.messages = data.messages;
        this.state.skillIds = data.skill_ids || [];
        this.state.running = data.state === "running";
        this.state.ready = true;
    }

    async newChat() {
        if (this.state.sessionId) {
            this.aiBus.unregister(this.state.sessionId);
            // Drop the outgoing conversation if it was never used.
            this._discardIfEmpty(this.state.sessionId);
        }
        this.state.sessionId = null;
        this.state.messages = [];
        this.state.skillIds = [];
        await this._startSession();
    }

    // Best-effort cleanup of a session that has no messages. The server only
    // deletes it when it is genuinely empty, so an active chat is never lost.
    async _discardIfEmpty(id) {
        if (!id) {
            return;
        }
        try {
            await rpc("/aidoo/discard", { session_id: id });
        } catch {
            // Non-fatal: a stale empty will be pruned next time the app opens.
        }
    }

    _bind(id) {
        if (this.state.sessionId) {
            this.aiBus.unregister(this.state.sessionId);
        }
        this.state.sessionId = id;
        this.aiBus.register(id, (p) => this._onBusEvent(p));
    }

    // ------------------------------------------------------------------
    // Messaging
    // ------------------------------------------------------------------
    async onSend(text, files = []) {
        this.state.authError = "";
        this.state.messages.push({
            id: `tmp-${Date.now()}`, role: "user", body: text,
            tool_calls: [], attachments: files,
        });
        this.state.running = true;
        try {
            await rpc("/aidoo/send", {
                session_id: this.state.sessionId,
                body: text,
                attachment_ids: files.map((f) => f.id),
            });
        } catch (e) {
            this.state.running = false;
            this.state.authError = e.message?.data?.message || _t("Failed to send.");
        }
    }

    _onBusEvent(p) {
        switch (p.kind) {
            case "started":
                this.state.running = true;
                break;
            case "message":
                this._upsert(p.message);
                break;
            case "done":
                this.state.running = false;
                this._reload();
                break;
            case "error":
                this.state.running = false;
                this.state.messages.push({
                    id: `err-${Date.now()}`, role: "error",
                    body: p.error || _t("Something went wrong."), tool_calls: [],
                });
                break;
        }
    }

    _upsert(msg) {
        const idx = this.state.messages.findIndex((m) => m.id === msg.id);
        if (idx === -1) {
            this.state.messages.push(msg);
        } else {
            this.state.messages[idx] = msg;
        }
    }

    async _reload() {
        if (!this.state.sessionId) {
            return;
        }
        const data = await rpc("/aidoo/messages", { session_id: this.state.sessionId });
        this.state.messages = data.messages;
    }

    async setSkills(skillIds) {
        const ids = skillIds || [];
        this.state.skillIds = ids;
        if (this.state.sessionId) {
            await rpc("/aidoo/skill", { session_id: this.state.sessionId, skill_ids: ids });
        }
    }

    openArtifact(seg) {
        if (seg.artifact?.url) {
            window.open(seg.artifact.url, "_blank", "noopener,noreferrer");
            return;
        }
        const blob = new Blob([seg.html || ""], { type: "text/html" });
        const url = URL.createObjectURL(blob);
        window.open(url, "_blank", "noopener,noreferrer");
        setTimeout(() => URL.revokeObjectURL(url), 60000);
    }
}

registry.category("main_components").add("aidoo.floating_button", {
    Component: FloatingAidoo,
});
