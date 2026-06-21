odoo.define('aidoo.ChatAction', function (require) {
"use strict";
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
var AbstractAction = require('web.AbstractAction');
var core = require('web.core');
var rpc = require('web.rpc');
var AiMessageList = require('aidoo.MessageList');
var AiComposer = require('aidoo.Composer');
var ReportFrame = require('aidoo.ReportFrame');
var artifacts = require('aidoo.artifacts');

// JSON-RPC helper against the module's controllers (auth=user routes).
function call(route, params) {
    return rpc.query({ route: route, params: params || {} });
}

var AiChatAction = AbstractAction.extend({
    template: 'aidoo.ChatAction',
    hasControlPanel: false,
    events: {
        'change .o_ai_provider_select': '_onProviderChange',
        'click .o_ai_new': '_onNewSession',
        'input .o_ai_search_input': '_onSearchInput',
        'click .o_ai_fav_filter': '_onToggleFavFilter',
        'click .o_ai_session_item': '_onOpenSession',
        'click .o_ai_session_star': '_onToggleFavorite',
        'click .o_ai_auth_start': '_startAuth',
        'click .o_ai_auth_complete': '_completeAuth',
        'keydown .o_ai_auth_code': '_onAuthCodeKey',
        'click .o_ai_device_start': '_startDeviceAuth',
        'click .o_ai_import_submit': '_submitImport',
        'click .o_ai_key_submit': '_submitKey',
        'click .o_ai_stop': 'onStop',
        'click .o_ai_artifact_newtab': 'openArtifactNewTab',
        'click .o_ai_artifact_close': 'closeArtifact',
    },

    init: function (parent, action) {
        this._super.apply(this, arguments);
        this.state = {
            providers: [],
            selectedProvider: "claude",
            sessions: [],
            searchQuery: "",
            favoritesOnly: false,
            currentId: null,
            currentProvider: "claude",
            messages: [],
            running: false,
            ready: false,
            authError: "",
            authStep: "idle",      // oauth_code: idle -> awaiting_code -> exchanging
            deviceUrl: "",
            deviceCode: "",
            polling: false,
            artifact: null,
            artifactOpen: false,
            contextLabel: "",
        };
        var params = (action && action.params) || {};
        this._initContext = params.context_model
            ? { context_model: params.context_model, context_res_id: params.context_res_id }
            : null;
    },

    willStart: function () {
        var self = this;
        return $.when(this._super.apply(this, arguments)).then(function () {
            return call("/aidoo/providers").then(function (info) {
                self.state.providers = info.providers;
                self.state.selectedProvider = info.default;
                return self._initChat();
            }).then(function () {
                self.state.ready = true;
            });
        });
    },

    start: function () {
        var sup = this._super.apply(this, arguments);
        this._mountChildren();
        return sup;
    },

    destroy: function () {
        this._stopPolling();
        if (this.state.currentId) {
            this.call('aidoo', 'unregister', this.state.currentId);
        }
        this._super.apply(this, arguments);
    },

    // ------------------------------------------------------------------
    // Rendering: shell (own template) + child widgets in mount points
    // ------------------------------------------------------------------
    _destroyChildren: function () {
        if (this.messageList) { this.messageList.destroy(); this.messageList = null; }
        if (this.composer) { this.composer.destroy(); this.composer = null; }
        if (this.reportFrame) { this.reportFrame.destroy(); this.reportFrame = null; }
    },

    _mountChildren: function () {
        if (this.isAuthed()) {
            this.messageList = new AiMessageList(this, {
                messages: this.state.messages,
                running: this.state.running,
                onOpenArtifact: this.openArtifact.bind(this),
            });
            this.messageList.appendTo(this.$('.o_ai_msglist_mount'));
            this.composer = new AiComposer(this, {
                sessionId: this.state.currentId,
                disabled: this.state.running,
                onSend: this.onSend.bind(this),
            });
            this.composer.appendTo(this.$('.o_ai_composer_mount'));
        }
        if (this.state.artifactOpen && this.state.artifact) {
            this.reportFrame = new ReportFrame(this, { html: this.state.artifact });
            this.reportFrame.appendTo(this.$('.o_ai_artifact_body'));
        }
    },

    // Full shell re-render (used for infrequent structural changes).
    _renderShell: function () {
        if (!this.state.ready) {
            return;
        }
        this._destroyChildren();
        this.renderElement();
        this._mountChildren();
    },

    // Cheap per-delta update of just the message list.
    _renderMessages: function () {
        if (this.messageList) {
            this.messageList.update(this.state.messages, this.state.running);
        } else {
            this._renderShell();
        }
    },

    _renderSidebar: function () {
        this.$('.o_ai_sidebar_list').html(
            core.qweb.render('aidoo.SidebarList', { widget: this }));
    },

    // ------------------------------------------------------------------
    // Provider helpers (called from the template)
    // ------------------------------------------------------------------
    _provider: function (code) {
        return this.state.providers.find(function (p) { return p.code === code; }) || {};
    },

    currentProvider: function () {
        return this._provider(this.state.currentProvider);
    },

    isAuthed: function () {
        return !!this.currentProvider().authenticated;
    },

    _setAuthed: function (code, value) {
        var p = this._provider(code);
        if (p) {
            p.authenticated = value;
        }
    },

    _resetAuthForms: function () {
        this.state.authError = "";
        this.state.authStep = "idle";
        this.state.deviceUrl = "";
        this.state.deviceCode = "";
        this._stopPolling();
    },

    // ------------------------------------------------------------------
    // Authentication gate — dispatched by the provider's auth_mode
    // ------------------------------------------------------------------
    _authMsg: function (e, fallback) {
        return (e && e.message && e.message.data && e.message.data.message) || fallback;
    },

    _startAuth: function () {
        var self = this;
        this.state.authError = "";
        call("/aidoo/auth/start", { provider: this.state.currentProvider }).then(function (res) {
            window.open(res.url, "_blank", "noopener,noreferrer");
            self.state.authStep = "awaiting_code";
            self._renderShell();
        }).guardedCatch(function (e) {
            self.state.authError = self._authMsg(e, "Could not start the login.");
            self._renderShell();
        });
    },

    _onAuthCodeKey: function (ev) {
        if (ev.key === "Enter") {
            this._completeAuth();
        }
    },

    _completeAuth: function () {
        var self = this;
        var code = (this.$('.o_ai_auth_code').val() || "").trim();
        if (!code) {
            this.state.authError = "Paste the code first.";
            this._renderShell();
            return;
        }
        this.state.authError = "";
        this.state.authStep = "exchanging";
        this._renderShell();
        call("/aidoo/auth/complete", { provider: this.state.currentProvider, code: code })
            .then(function () {
                self._onAuthSuccess();
            }).guardedCatch(function (e) {
                self.state.authStep = "awaiting_code";
                self.state.authError = self._authMsg(e, "Authentication failed. Try again.");
                self._renderShell();
            });
    },

    _startDeviceAuth: function () {
        var self = this;
        this.state.authError = "";
        call("/aidoo/auth/start", { provider: this.state.currentProvider }).then(function (res) {
            if (res.authenticated) {
                self._onAuthSuccess();
                return;
            }
            self.state.deviceUrl = res.url || "";
            self.state.deviceCode = res.code || "";
            if (self.state.deviceUrl) {
                window.open(self.state.deviceUrl, "_blank", "noopener,noreferrer");
            }
            self._startPolling();
            self._renderShell();
        }).guardedCatch(function (e) {
            self.state.authError = self._authMsg(e, "Could not start the login.");
            self._renderShell();
        });
    },

    _startPolling: function () {
        var self = this;
        this._stopPolling();
        this.state.polling = true;
        this._authPoll = setInterval(function () {
            call("/aidoo/auth/status", { provider: self.state.currentProvider })
                .then(function (res) {
                    if (res.authenticated) {
                        self._onAuthSuccess();
                    }
                }).guardedCatch(function () {
                    // keep polling; transient errors are non-fatal
                });
        }, 2500);
    },

    _stopPolling: function () {
        if (this._authPoll) {
            clearInterval(this._authPoll);
            this._authPoll = null;
        }
        this.state.polling = false;
    },

    _submitImport: function () {
        var self = this;
        var creds = (this.$('.o_ai_import_creds').val() || "").trim();
        if (!creds) {
            this.state.authError = "Paste your credentials JSON.";
            this._renderShell();
            return;
        }
        this.state.authError = "";
        call("/aidoo/auth/import", { provider: this.state.currentProvider, creds: creds })
            .then(function () {
                self._onAuthSuccess();
            }).guardedCatch(function (e) {
                self.state.authError = self._authMsg(e, "Import failed.");
                self._renderShell();
            });
    },

    _submitKey: function () {
        var self = this;
        var key = (this.$('.o_ai_key_input').val() || "").trim();
        if (!key) {
            this.state.authError = "Paste an API key.";
            this._renderShell();
            return;
        }
        this.state.authError = "";
        call("/aidoo/auth/key", { provider: this.state.currentProvider, key: key })
            .then(function (res) {
                if (res.authenticated) {
                    self._onAuthSuccess();
                } else {
                    self.state.authError = "The key was not accepted.";
                    self._renderShell();
                }
            }).guardedCatch(function (e) {
                self.state.authError = self._authMsg(e, "Could not save the key.");
                self._renderShell();
            });
    },

    _onAuthSuccess: function () {
        this._setAuthed(this.state.currentProvider, true);
        this._resetAuthForms();
        return this._openSession(this.state.currentId);
    },

    // ------------------------------------------------------------------
    // Conversations
    // ------------------------------------------------------------------
    _initChat: function () {
        var self = this;
        return this._loadSessions().then(function () {
            if (self._initContext) {
                var ctx = self._initContext;
                self._initContext = null;
                return self._newSession(ctx);
            }
            if (!self.state.sessions.length) {
                return self._newSession();
            }
            return self._openSession(self.state.sessions[0].id);
        });
    },

    _loadSessions: function () {
        var self = this;
        return call("/aidoo/sessions").then(function (sessions) {
            self.state.sessions = sessions;
        });
    },

    // Sidebar list after search/favorites filter, starred sorted to the top.
    filteredSessions: function () {
        var q = this.state.searchQuery.trim().toLowerCase();
        var list = this.state.sessions;
        if (q) {
            list = list.filter(function (s) {
                return (s.name || "").toLowerCase().indexOf(q) !== -1;
            });
        }
        if (this.state.favoritesOnly) {
            list = list.filter(function (s) { return s.is_favorite; });
        }
        return list.slice().sort(function (a, b) {
            return (b.is_favorite ? 1 : 0) - (a.is_favorite ? 1 : 0);
        });
    },

    _onProviderChange: function (ev) {
        this.state.selectedProvider = $(ev.currentTarget).val();
    },

    _onSearchInput: function (ev) {
        this.state.searchQuery = $(ev.currentTarget).val() || "";
        this._renderSidebar();
    },

    _onToggleFavFilter: function () {
        this.state.favoritesOnly = !this.state.favoritesOnly;
        this.$('.o_ai_fav_filter').toggleClass('active', this.state.favoritesOnly);
        this.$('.o_ai_fav_filter i')
            .toggleClass('fa-star', this.state.favoritesOnly)
            .toggleClass('fa-star-o', !this.state.favoritesOnly);
        this._renderSidebar();
    },

    _onToggleFavorite: function (ev) {
        ev.stopPropagation();
        var id = $(ev.currentTarget).data('id');
        var session = this.state.sessions.find(function (s) { return s.id === id; });
        if (!session) {
            return;
        }
        session.is_favorite = !session.is_favorite;
        this._renderSidebar();
        call("/aidoo/favorite", { session_id: session.id, is_favorite: session.is_favorite });
    },

    _onNewSession: function () {
        this._newSession();
    },

    _newSession: function (context) {
        var self = this;
        var ctx = context && context.context_model ? context : null;
        var params = Object.assign({ provider: this.state.selectedProvider }, ctx || {});
        return call("/aidoo/new", params).then(function (s) {
            self.state.sessions.unshift(s);
            return self._openSession(s.id);
        });
    },

    _onOpenSession: function (ev) {
        this._openSession($(ev.currentTarget).data('id'));
    },

    _openSession: function (id) {
        var self = this;
        if (this.state.currentId && this.state.currentId !== id) {
            this.call('aidoo', 'unregister', this.state.currentId);
        }
        this._resetAuthForms();
        this.state.currentId = id;
        this.call('aidoo', 'register', id, function (p) { self._onBusEvent(p); });
        return call("/aidoo/messages", { session_id: id }).then(function (data) {
            self.state.currentProvider = data.provider;
            self.state.contextLabel = data.context_label || "";
            self.state.messages = data.messages;
            self.state.running = data.state === "running";
            self.state.artifact = null;
            self.state.artifactOpen = false;
            self._captureLatestArtifact();
            self._renderShell();
        });
    },

    // ------------------------------------------------------------------
    // Artifact panel
    // ------------------------------------------------------------------
    openArtifact: function (html) {
        this.state.artifact = html;
        this.state.artifactOpen = true;
        this._renderShell();
    },

    closeArtifact: function () {
        this.state.artifactOpen = false;
        this._renderShell();
    },

    openArtifactNewTab: function () {
        if (!this.state.artifact) {
            return;
        }
        var blob = new Blob([this.state.artifact], { type: "text/html" });
        var url = URL.createObjectURL(blob);
        window.open(url, "_blank", "noopener,noreferrer");
        setTimeout(function () { URL.revokeObjectURL(url); }, 60000);
    },

    _messageArtifact: function (msg) {
        if (!msg) {
            return null;
        }
        if (msg.role === "report") {
            return msg.body || null;
        }
        if (msg.role === "assistant") {
            var blocks = artifacts.extractHtmlBlocks(msg.body || "");
            return blocks.length ? blocks[blocks.length - 1] : null;
        }
        return null;
    },

    _maybeCaptureArtifact: function (msg) {
        var html = this._messageArtifact(msg);
        if (html) {
            this.openArtifact(html);
        }
    },

    _captureLatestArtifact: function () {
        for (var i = this.state.messages.length - 1; i >= 0; i--) {
            var html = this._messageArtifact(this.state.messages[i]);
            if (html) {
                this.state.artifact = html;
                this.state.artifactOpen = true;
                return;
            }
        }
    },

    _upsertMessage: function (msg) {
        var idx = this.state.messages.findIndex(function (m) { return m.id === msg.id; });
        if (idx === -1) {
            this.state.messages.push(msg);
        } else {
            this.state.messages[idx] = msg;
        }
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
                this._upsertMessage(p.message);
                this._renderMessages();
                this._maybeCaptureArtifact(p.message);
                break;
            case "done":
                this.state.running = false;
                this._reload();
                break;
            case "error":
                this.state.running = false;
                this.state.messages.push({
                    id: "err-" + Date.now(),
                    role: "error",
                    body: p.error || "Something went wrong.",
                    tool_calls: [],
                });
                this._renderShell();
                break;
        }
    },

    _reload: function () {
        var self = this;
        if (!this.state.currentId) {
            this._renderShell();
            return;
        }
        call("/aidoo/messages", { session_id: this.state.currentId }).then(function (data) {
            self.state.messages = data.messages;
            self._renderShell();
        });
    },

    onSend: function (text, files) {
        var self = this;
        files = files || [];
        this.state.messages.push({
            id: "tmp-" + Date.now(),
            role: "user",
            body: text,
            tool_calls: [],
            attachments: files,
        });
        this.state.running = true;
        this._renderShell();
        call("/aidoo/send", {
            session_id: this.state.currentId,
            body: text,
            attachment_ids: files.map(function (f) { return f.id; }),
        }).guardedCatch(function (e) {
            self.state.running = false;
            self.do_warn("Aidoo", self._authMsg(e, "Failed to send."));
            self._renderShell();
        });
    },

    onStop: function () {
        var self = this;
        call("/aidoo/stop", { session_id: this.state.currentId }).then(function () {
            self.state.running = false;
            self._renderShell();
        });
    },
});

core.action_registry.add('aidoo.chat', AiChatAction);

return AiChatAction;
});
