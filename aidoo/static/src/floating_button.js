odoo.define('aidoo.floating_button', function (require) {
"use strict";
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).

// Floating "Ask Aidoo" companion shown on form and list views. The button
// (bottom-right) toggles a chat panel that floats over the current view. Seeded
// with the current view's context (record on a form, filtered set on a list).
// v18 mounted it via the main_components registry; Odoo 13 has none, so we mount
// it once into the WebClient (see the include at the bottom).
var Widget = require('web.Widget');
var WebClient = require('web.WebClient');
var core = require('web.core');
var rpc = require('web.rpc');
var session = require('web.session');
var AiMessageList = require('aidoo.MessageList');
var AiComposer = require('aidoo.Composer');

var _t = core._t;

function call(route, params) {
    return rpc.query({ route: route, params: params || {} });
}

var FloatingAidoo = Widget.extend({
    template: 'aidoo.FloatingButton',
    events: {
        'click .o_aidoo_fab_btn': 'toggle',
        'click .o_aidoo_new': 'newChat',
        'click .o_aidoo_expand': 'toggleExpand',
        'click .o_aidoo_min': 'minimize',
        'click .o_aidoo_close': 'close',
    },

    init: function (parent) {
        this._super.apply(this, arguments);
        this.current = null;     // current view context from the service
        this._allowed = false;
        this.state = {
            open: false,
            expanded: false,
            ready: false,
            contextLabel: "",
            sessionId: null,
            messages: [],
            running: false,
            authError: "",
        };
    },

    willStart: function () {
        var self = this;
        return $.when(this._super.apply(this, arguments)).then(function () {
            return session.user_has_group('aidoo.group_ai_user').then(function (r) {
                self._allowed = r;
            });
        });
    },

    start: function () {
        var sup = this._super.apply(this, arguments);
        // React to view changes published by the controller patches.
        core.bus.on('aidoo_view_context:changed', this, this._onViewContextChanged);
        // Seed from whatever the service currently holds.
        try {
            this.current = this.call('aidoo_view_context', 'get');
        } catch (e) {
            this.current = null;
        }
        this._renderShell();
        return sup;
    },

    destroy: function () {
        core.bus.off('aidoo_view_context:changed', this);
        if (this.state.sessionId) {
            this.call('aidoo', 'unregister', this.state.sessionId);
            this._discardIfEmpty(this.state.sessionId);
        }
        this._super.apply(this, arguments);
    },

    // ------------------------------------------------------------------
    // Visibility (called from the template)
    // ------------------------------------------------------------------
    showFab: function () {
        return this._allowed && !!this.current;
    },

    panelOpen: function () {
        return this.showFab() && this.state.open;
    },

    _onViewContextChanged: function (ctx) {
        this.current = ctx;
        // While the panel is open the conversation persists; only refresh the FAB
        // when it's closed so we don't tear down an active chat.
        if (!this.state.open) {
            this._renderShell();
        }
    },

    // Context payload describing what the user is looking at.
    _payload: function () {
        var c = this.current;
        if (!c) {
            return {};
        }
        var p = { context_model: c.resModel };
        if (c.viewType === "form" && c.resId) {
            p.context_res_id = c.resId;
        } else if (c.viewType === "list" && c.domain) {
            p.context_domain = JSON.stringify(c.domain);
        }
        return p;
    },

    // ------------------------------------------------------------------
    // Rendering
    // ------------------------------------------------------------------
    _destroyChildren: function () {
        if (this.messageList) { this.messageList.destroy(); this.messageList = null; }
        if (this.composer) { this.composer.destroy(); this.composer = null; }
    },

    _mountChildren: function () {
        if (this.panelOpen() && this.state.ready) {
            this.messageList = new AiMessageList(this, {
                messages: this.state.messages,
                running: this.state.running,
                onOpenArtifact: this.openArtifact.bind(this),
            });
            this.messageList.appendTo(this.$('.o_aidoo_msglist_mount'));
            this.composer = new AiComposer(this, {
                sessionId: this.state.sessionId,
                disabled: this.state.running,
                onSend: this.onSend.bind(this),
            });
            this.composer.appendTo(this.$('.o_aidoo_composer_mount'));
        }
    },

    _renderShell: function () {
        this._destroyChildren();
        this.renderElement();
        this._mountChildren();
    },

    _renderMessages: function () {
        if (this.messageList) {
            this.messageList.update(this.state.messages, this.state.running);
        } else {
            this._renderShell();
        }
    },

    // ------------------------------------------------------------------
    // Panel controls
    // ------------------------------------------------------------------
    toggle: function () {
        var self = this;
        this.state.open = !this.state.open;
        if (this.state.open && !this.state.sessionId) {
            this._renderShell();
            return this._startSession();
        }
        this._renderShell();
    },

    minimize: function () {
        this.state.open = false;
        this._renderShell();
    },

    toggleExpand: function () {
        this.state.expanded = !this.state.expanded;
        this._renderShell();
    },

    close: function () {
        this.state.open = false;
        this.state.expanded = false;
        if (this.state.sessionId) {
            this.call('aidoo', 'unregister', this.state.sessionId);
            this._discardIfEmpty(this.state.sessionId);
        }
        this.state.sessionId = null;
        this.state.messages = [];
        this.state.contextLabel = "";
        this._renderShell();
    },

    // ------------------------------------------------------------------
    // Session lifecycle
    // ------------------------------------------------------------------
    _startSession: function () {
        var self = this;
        this.state.ready = false;
        this.state.authError = "";
        this._renderShell();
        return call("/aidoo/new", this._payload()).then(function (s) {
            self._bind(s.id);
            self.state.contextLabel = s.context_label || "";
            return call("/aidoo/messages", { session_id: s.id }).then(function (data) {
                self.state.messages = data.messages;
                self.state.running = data.state === "running";
                self.state.ready = true;
                self._renderShell();
            });
        });
    },

    newChat: function () {
        if (this.state.sessionId) {
            this.call('aidoo', 'unregister', this.state.sessionId);
            this._discardIfEmpty(this.state.sessionId);
        }
        this.state.sessionId = null;
        this.state.messages = [];
        this._startSession();
    },

    // Best-effort cleanup of a session with no messages. The server only deletes
    // it when genuinely empty, so an active chat is never lost.
    _discardIfEmpty: function (id) {
        if (!id) {
            return;
        }
        call("/aidoo/discard", { session_id: id }).guardedCatch(function () {
            // Non-fatal: a stale empty is pruned next time the app opens.
        });
    },

    _bind: function (id) {
        var self = this;
        if (this.state.sessionId) {
            this.call('aidoo', 'unregister', this.state.sessionId);
        }
        this.state.sessionId = id;
        this.call('aidoo', 'register', id, function (p) { self._onBusEvent(p); });
    },

    // ------------------------------------------------------------------
    // Messaging
    // ------------------------------------------------------------------
    onSend: function (text, files) {
        var self = this;
        files = files || [];
        this.state.authError = "";
        this.state.messages.push({
            id: "tmp-" + Date.now(), role: "user", body: text,
            tool_calls: [], attachments: files,
        });
        this.state.running = true;
        this._renderShell();
        call("/aidoo/send", {
            session_id: this.state.sessionId,
            body: text,
            attachment_ids: files.map(function (f) { return f.id; }),
        }).guardedCatch(function (e) {
            self.state.running = false;
            self.state.authError = (e && e.message && e.message.data && e.message.data.message)
                || _t("Failed to send.");
            self._renderShell();
        });
    },

    _onBusEvent: function (p) {
        switch (p.kind) {
            case "started":
                if (!this.state.running) {
                    this.state.running = true;
                    this._renderShell();
                }
                break;
            case "message":
                this._upsert(p.message);
                this._renderMessages();
                break;
            case "done":
                this.state.running = false;
                this._reload();
                break;
            case "error":
                this.state.running = false;
                this.state.messages.push({
                    id: "err-" + Date.now(), role: "error",
                    body: p.error || _t("Something went wrong."), tool_calls: [],
                });
                this._renderShell();
                break;
        }
    },

    _upsert: function (msg) {
        var idx = this.state.messages.findIndex(function (m) { return m.id === msg.id; });
        if (idx === -1) {
            this.state.messages.push(msg);
        } else {
            this.state.messages[idx] = msg;
        }
    },

    _reload: function () {
        var self = this;
        if (!this.state.sessionId) {
            this._renderShell();
            return;
        }
        call("/aidoo/messages", { session_id: this.state.sessionId }).then(function (data) {
            self.state.messages = data.messages;
            self._renderShell();
        });
    },

    openArtifact: function (html) {
        var blob = new Blob([html], { type: "text/html" });
        var url = URL.createObjectURL(blob);
        window.open(url, "_blank", "noopener,noreferrer");
        setTimeout(function () { URL.revokeObjectURL(url); }, 60000);
    },
});

// Mount the floating widget once into the web client (no main_components in v13).
WebClient.include({
    show_application: function () {
        var res = this._super.apply(this, arguments);
        if (!this._aidooFloating) {
            this._aidooFloating = new FloatingAidoo(this);
            this._aidooFloating.appendTo(this.$el);
        }
        return res;
    },
});

return FloatingAidoo;
});
