odoo.define('aidoo.MessageList', function (require) {
"use strict";
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
var Widget = require('web.Widget');
var MessageMixin = require('aidoo.message');

var AiMessageList = Widget.extend(MessageMixin, {
    template: 'aidoo.MessageList',
    events: {
        'click .o_ai_tools_toggle': '_onToggleTools',
        'click .o_ai_artifact_card': '_onArtifactCard',
    },

    /**
     * @param {Widget} parent
     * @param {Object} options {onOpenArtifact}
     */
    init: function (parent, options) {
        this._super.apply(this, arguments);
        options = options || {};
        this.messages = options.messages || [];
        this.running = !!options.running;
        this.onOpenArtifact = options.onOpenArtifact || function () {};
        this._toolsOpen = new Set();
    },

    start: function () {
        this._scrollToBottom();
        return this._super.apply(this, arguments);
    },

    /**
     * Refresh the rendered list and stick to the bottom.
     */
    update: function (messages, running) {
        this.messages = messages || [];
        this.running = !!running;
        this.renderElement();
        this._scrollToBottom();
    },

    // A single turn often arrives as several assistant messages (each tagged with
    // its own id), many carrying only tool calls and no text. Merge consecutive
    // assistant messages into one bubble so their tool calls share a single fold.
    displayMessages: function () {
        var out = [];
        this.messages.forEach(function (msg) {
            var last = out[out.length - 1];
            if (msg.role === "assistant" && last && last.role === "assistant") {
                var bodies = [last.body, msg.body].filter(function (b) { return b && b.trim(); });
                last.body = bodies.join("\n\n");
                last.tool_calls = (last.tool_calls || []).concat(msg.tool_calls || []);
                last.attachments = (last.attachments || []).concat(msg.attachments || []);
            } else {
                out.push(Object.assign({}, msg)); // shallow clone; never mutate upstream
            }
        });
        return out;
    },

    _findMessage: function (id) {
        var merged = this.displayMessages();
        for (var i = 0; i < merged.length; i++) {
            if (String(merged[i].id) === String(id)) {
                return merged[i];
            }
        }
        return null;
    },

    _scrollToBottom: function () {
        var el = this.el;
        if (el) {
            el.scrollTop = el.scrollHeight;
        }
    },

    _onToggleTools: function (ev) {
        ev.preventDefault();
        var $btn = $(ev.currentTarget);
        var id = $btn.data('msgId');
        if (this._toolsOpen.has(id)) {
            this._toolsOpen.delete(id);
        } else {
            this._toolsOpen.add(id);
        }
        $btn.siblings('.o_ai_tools_list').toggleClass('d-none');
        $btn.find('.o_ai_caret').toggleClass('fa-caret-right fa-caret-down');
    },

    _onArtifactCard: function (ev) {
        ev.preventDefault();
        var $card = $(ev.currentTarget);
        var msg = this._findMessage($card.data('msgId'));
        if (!msg) {
            return;
        }
        var seg = this.segments(msg)[$card.data('segIndex')];
        if (seg && seg.html) {
            this.onOpenArtifact(seg.html);
        }
    },
});

return AiMessageList;
});
