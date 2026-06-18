/** @odoo-module **/
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import { Component, useState, onWillStart, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { rpc } from "@web/core/network/rpc";
import { browser } from "@web/core/browser/browser";
import { AiMessageList } from "./components/message_list";
import { AiComposer } from "./components/composer";
import { ReportFrame } from "./components/report_frame";
import { extractHtmlBlocks } from "./artifacts";

export class AiChatAction extends Component {
    static template = "aidoo.ChatAction";
    static components = { AiMessageList, AiComposer, ReportFrame };
    static props = ["*"];

    setup() {
        this.aiBus = useService("aidoo");
        this.notification = useService("notification");
        this.state = useState({
            providers: [],          // [{code,label,accent,auth_mode,authenticated}]
            selectedProvider: "claude",  // provider for the next new conversation
            sessions: [],
            currentId: null,
            currentProvider: "claude",
            messages: [],
            running: false,
            ready: false,
            // Auth gate (scoped to the current conversation's provider)
            authError: "",
            authStep: "idle",       // oauth_code: idle -> awaiting_code -> exchanging
            authCode: "",           // oauth_code: pasted code
            deviceUrl: "",          // device_poll: verification URL
            deviceCode: "",         // device_poll: one-time code
            polling: false,         // device_poll: waiting for completion
            credsInput: "",         // import_or_key: pasted credentials blob
            apiKeyInput: "",        // import_or_key: API key
            // Artifact panel (right side): rendered chart/report HTML.
            artifact: null,
            artifactOpen: false,
            // Focus record when opened from a form via "Ask Aidoo".
            contextLabel: "",
        });

        // When opened from "Ask Aidoo" on a record, params carry the focus record.
        const params = this.props.action?.params || this.props.params || {};
        this._initContext = params.context_model
            ? { context_model: params.context_model, context_res_id: params.context_res_id }
            : null;

        onWillStart(async () => {
            const info = await rpc("/aidoo/providers");
            this.state.providers = info.providers;
            this.state.selectedProvider = info.default;
            await this._initChat();
            this.state.ready = true;
        });

        onWillUnmount(() => {
            this._stopPolling();
            if (this.state.currentId) {
                this.aiBus.unregister(this.state.currentId);
            }
        });
    }

    // ------------------------------------------------------------------
    // Provider helpers
    // ------------------------------------------------------------------
    _provider(code) {
        return this.state.providers.find((p) => p.code === code) || {};
    }

    get currentProvider() {
        return this._provider(this.state.currentProvider);
    }

    get isAuthed() {
        return !!this.currentProvider.authenticated;
    }

    _setAuthed(code, value) {
        const p = this._provider(code);
        if (p) {
            p.authenticated = value;
        }
    }

    _resetAuthForms() {
        this.state.authError = "";
        this.state.authStep = "idle";
        this.state.authCode = "";
        this.state.deviceUrl = "";
        this.state.deviceCode = "";
        this.state.credsInput = "";
        this.state.apiKeyInput = "";
        this._stopPolling();
    }

    // ------------------------------------------------------------------
    // Authentication gate — dispatched by the provider's auth_mode
    // ------------------------------------------------------------------
    // oauth_code (Claude)
    async _startAuth() {
        this.state.authError = "";
        try {
            const { url } = await rpc("/aidoo/auth/start", {
                provider: this.state.currentProvider,
            });
            window.open(url, "_blank", "noopener,noreferrer");
            this.state.authStep = "awaiting_code";
        } catch (e) {
            this.state.authError = e.message?.data?.message || "Could not start the login.";
        }
    }

    async _completeAuth() {
        const code = (this.state.authCode || "").trim();
        if (!code) {
            this.state.authError = "Paste the code first.";
            return;
        }
        this.state.authError = "";
        this.state.authStep = "exchanging";
        try {
            await rpc("/aidoo/auth/complete", {
                provider: this.state.currentProvider, code,
            });
            await this._onAuthSuccess();
        } catch (e) {
            this.state.authStep = "awaiting_code";
            this.state.authError = e.message?.data?.message || "Authentication failed. Try again.";
        }
    }

    // device_poll (Codex)
    async _startDeviceAuth() {
        this.state.authError = "";
        try {
            const res = await rpc("/aidoo/auth/start", {
                provider: this.state.currentProvider,
            });
            if (res.authenticated) {
                await this._onAuthSuccess();
                return;
            }
            this.state.deviceUrl = res.url || "";
            this.state.deviceCode = res.code || "";
            if (this.state.deviceUrl) {
                window.open(this.state.deviceUrl, "_blank", "noopener,noreferrer");
            }
            this._startPolling();
        } catch (e) {
            this.state.authError = e.message?.data?.message || "Could not start the login.";
        }
    }

    _startPolling() {
        this._stopPolling();
        this.state.polling = true;
        this._authPoll = browser.setInterval(async () => {
            try {
                const { authenticated } = await rpc("/aidoo/auth/status", {
                    provider: this.state.currentProvider,
                });
                if (authenticated) {
                    await this._onAuthSuccess();
                }
            } catch {
                // keep polling; transient errors are non-fatal
            }
        }, 2500);
    }

    _stopPolling() {
        if (this._authPoll) {
            browser.clearInterval(this._authPoll);
            this._authPoll = null;
        }
        this.state.polling = false;
    }

    // import_or_key (Gemini / Grok)
    async _submitImport() {
        const creds = (this.state.credsInput || "").trim();
        if (!creds) {
            this.state.authError = "Paste your credentials JSON.";
            return;
        }
        this.state.authError = "";
        try {
            await rpc("/aidoo/auth/import", {
                provider: this.state.currentProvider, creds,
            });
            await this._onAuthSuccess();
        } catch (e) {
            this.state.authError = e.message?.data?.message || "Import failed.";
        }
    }

    async _submitKey() {
        const key = (this.state.apiKeyInput || "").trim();
        if (!key) {
            this.state.authError = "Paste an API key.";
            return;
        }
        this.state.authError = "";
        try {
            const res = await rpc("/aidoo/auth/key", {
                provider: this.state.currentProvider, key,
            });
            if (res.authenticated) {
                await this._onAuthSuccess();
            } else {
                this.state.authError = "The key was not accepted.";
            }
        } catch (e) {
            this.state.authError = e.message?.data?.message || "Could not save the key.";
        }
    }

    async _onAuthSuccess() {
        this._setAuthed(this.state.currentProvider, true);
        this._resetAuthForms();
        await this._openSession(this.state.currentId);
    }

    // ------------------------------------------------------------------
    // Conversations
    // ------------------------------------------------------------------
    async _initChat() {
        await this._loadSessions();
        if (this._initContext) {
            await this._newSession(this._initContext);
            this._initContext = null;  // only seed the very first conversation
        } else if (!this.state.sessions.length) {
            await this._newSession();
        } else {
            await this._openSession(this.state.sessions[0].id);
        }
    }

    async _loadSessions() {
        this.state.sessions = await rpc("/aidoo/sessions");
    }

    async _newSession(context = null) {
        // Also used as a click handler (receives the event) — only a real
        // context object carries context_model.
        const ctx = context && context.context_model ? context : null;
        const s = await rpc("/aidoo/new", {
            provider: this.state.selectedProvider,
            ...(ctx || {}),
        });
        this.state.sessions.unshift(s);
        await this._openSession(s.id);
    }

    async _openSession(id) {
        if (this.state.currentId && this.state.currentId !== id) {
            this.aiBus.unregister(this.state.currentId);
        }
        this._resetAuthForms();
        this.state.currentId = id;
        this.aiBus.register(id, (p) => this._onBusEvent(p));
        const data = await rpc("/aidoo/messages", { session_id: id });
        this.state.currentProvider = data.provider;
        this.state.contextLabel = data.context_label || "";
        this.state.messages = data.messages;
        this.state.running = data.state === "running";
        this.state.artifact = null;
        this.state.artifactOpen = false;
        this._captureLatestArtifact();
    }

    // ------------------------------------------------------------------
    // Artifact panel
    // ------------------------------------------------------------------
    openArtifact(html) {
        this.state.artifact = html;
        this.state.artifactOpen = true;
    }

    closeArtifact() {
        this.state.artifactOpen = false;
    }

    openArtifactNewTab() {
        if (!this.state.artifact) {
            return;
        }
        const blob = new Blob([this.state.artifact], { type: "text/html" });
        const url = URL.createObjectURL(blob);
        window.open(url, "_blank", "noopener,noreferrer");
        setTimeout(() => URL.revokeObjectURL(url), 60000);
    }

    _messageArtifact(msg) {
        if (!msg) {
            return null;
        }
        if (msg.role === "report") {
            return msg.body || null;
        }
        if (msg.role === "assistant") {
            const blocks = extractHtmlBlocks(msg.body || "");
            return blocks.length ? blocks[blocks.length - 1] : null;
        }
        return null;
    }

    _maybeCaptureArtifact(msg) {
        const html = this._messageArtifact(msg);
        if (html) {
            this.openArtifact(html);
        }
    }

    _captureLatestArtifact() {
        for (let i = this.state.messages.length - 1; i >= 0; i--) {
            const html = this._messageArtifact(this.state.messages[i]);
            if (html) {
                this.openArtifact(html);
                return;
            }
        }
    }

    _upsertMessage(msg) {
        const idx = this.state.messages.findIndex((m) => m.id === msg.id);
        if (idx === -1) {
            this.state.messages.push(msg);
        } else {
            this.state.messages[idx] = msg;
        }
    }

    _onBusEvent(p) {
        switch (p.kind) {
            case "started":
                this.state.running = true;
                break;
            case "message":
                this._upsertMessage(p.message);
                this._maybeCaptureArtifact(p.message);
                break;
            case "done":
                this.state.running = false;
                this._reload();
                break;
            case "error":
                this.state.running = false;
                this.state.messages.push({
                    id: `err-${Date.now()}`,
                    role: "error",
                    body: p.error || "Something went wrong.",
                    tool_calls: [],
                });
                break;
        }
    }

    async _reload() {
        if (!this.state.currentId) return;
        const data = await rpc("/aidoo/messages", {
            session_id: this.state.currentId,
        });
        this.state.messages = data.messages;
    }

    async onSend(text, files = []) {
        this.state.messages.push({
            id: `tmp-${Date.now()}`,
            role: "user",
            body: text,
            tool_calls: [],
            attachments: files,
        });
        this.state.running = true;
        try {
            await rpc("/aidoo/send", {
                session_id: this.state.currentId,
                body: text,
                attachment_ids: files.map((f) => f.id),
            });
        } catch (e) {
            this.state.running = false;
            this.notification.add(e.message?.data?.message || "Failed to send.", {
                type: "danger",
            });
        }
    }

    async onStop() {
        await rpc("/aidoo/stop", { session_id: this.state.currentId });
        this.state.running = false;
    }
}

registry.category("actions").add("aidoo.chat", AiChatAction);
