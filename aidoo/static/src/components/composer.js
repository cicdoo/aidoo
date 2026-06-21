odoo.define('aidoo.Composer', function (require) {
"use strict";
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
var Widget = require('web.Widget');
var session = require('web.session');

var AiComposer = Widget.extend({
    template: 'aidoo.Composer',
    events: {
        'click .o_ai_send': '_send',
        'click .o_ai_attach': 'pickFiles',
        'change .o_ai_file_input': 'onFilesChosen',
        'keydown .o_ai_composer_input': 'onKeydown',
        'input .o_ai_composer_input': '_onInput',
        'click .o_ai_attachment_remove': '_onRemoveFile',
    },

    /**
     * @param {Widget} parent
     * @param {Object} options {sessionId, disabled, onSend}
     */
    init: function (parent, options) {
        this._super.apply(this, arguments);
        options = options || {};
        this.sessionId = options.sessionId || null;
        this.disabled = !!options.disabled;
        this.onSend = options.onSend || function () {};
        // files: [{id, name, mimetype}] uploaded and queued for the next message.
        this.state = { files: [], uploading: false, error: "" };
    },

    start: function () {
        this._syncButtons();
        return this._super.apply(this, arguments);
    },

    setSessionId: function (id) {
        this.sessionId = id;
    },

    setDisabled: function (disabled) {
        this.disabled = !!disabled;
        this._syncButtons();
    },

    onKeydown: function (ev) {
        // Enter sends; Shift+Enter inserts a newline.
        if (ev.key === "Enter" && !ev.shiftKey) {
            ev.preventDefault();
            this._send();
        }
    },

    _text: function () {
        return (this.$('.o_ai_composer_input').val() || "").trim();
    },

    _canSend: function () {
        return (
            !this.disabled &&
            !this.state.uploading &&
            (!!this._text() || this.state.files.length > 0)
        );
    },

    _syncButtons: function () {
        this.$('.o_ai_send').prop('disabled', !this._canSend());
        this.$('.o_ai_attach').prop('disabled', this.disabled || this.state.uploading);
    },

    _onInput: function (ev) {
        this.autoGrow(ev);
        this._syncButtons();
    },

    pickFiles: function () {
        if (this.disabled || this.state.uploading) {
            return;
        }
        this.$('.o_ai_file_input').click();
    },

    onFilesChosen: function (ev) {
        var self = this;
        var files = Array.prototype.slice.call(ev.target.files || []);
        ev.target.value = ""; // allow re-picking the same file
        if (!files.length || !this.sessionId) {
            return;
        }
        this.state.error = "";
        this.state.uploading = true;
        this._renderState();

        var form = new FormData();
        form.append("session_id", this.sessionId);
        form.append("csrf_token", session.csrf_token || odoo.csrf_token);
        files.forEach(function (f) { form.append("ufile", f); });

        return fetch("/aidoo/upload", { method: "POST", body: form })
            .then(function (resp) {
                return resp.json().then(function (data) { return { resp: resp, data: data }; });
            })
            .then(function (r) {
                if (!r.resp.ok || r.data.error) {
                    self.state.error = r.data.error || "Upload failed.";
                } else {
                    self.state.files = self.state.files.concat(r.data.attachments || []);
                }
            })
            .catch(function () {
                self.state.error = "Upload failed.";
            })
            .then(function () {
                self.state.uploading = false;
                self._renderState();
            });
    },

    _onRemoveFile: function (ev) {
        var id = $(ev.currentTarget).data('id');
        this.state.files = this.state.files.filter(function (f) { return f.id !== id; });
        this._renderState();
    },

    // Re-render keeping the typed text (chips/error/spinner changed).
    _renderState: function () {
        var text = this.$('.o_ai_composer_input').val() || "";
        this.renderElement();
        this.$('.o_ai_composer_input').val(text);
        this._syncButtons();
    },

    _send: function () {
        if (!this._canSend()) {
            return;
        }
        var text = this._text();
        var files = this.state.files;
        this.onSend(text, files);
        this.state.files = [];
        this.state.error = "";
        this.renderElement();
        this._syncButtons();
    },

    autoGrow: function (ev) {
        var el = ev.target;
        el.style.height = "auto";
        el.style.height = Math.min(el.scrollHeight, 200) + "px";
    },
});

return AiComposer;
});
