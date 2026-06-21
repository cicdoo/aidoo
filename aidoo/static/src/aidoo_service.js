odoo.define('aidoo.service', function (require) {
"use strict";
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).

// Fans the Aidoo bus payloads out to the currently-open chat widget(s), keyed by
// session_id. Odoo 13 has no notification-type subscribe (v18); the backend sends
// on the user's partner channel (db, 'res.partner', partner_id) — see
// aidoo.runner._emit — which the mail module already polls server-side (its
// _poll override appends that channel). We must NOT addChannel it ourselves: the
// v13 /longpolling/poll endpoint rejects non-string (tuple/array) channels and
// would break all polling. We just listen and filter by our ``_aidoo`` marker.
var AbstractService = require('web.AbstractService');
var core = require('web.core');

var AidooService = AbstractService.extend({
    dependencies: ['bus_service'],

    start: function () {
        this._super.apply(this, arguments);
        this._handlers = {}; // session_id -> callback
        this.call('bus_service', 'startPolling');
        this.call('bus_service', 'onNotification', this, this._onNotification);
    },

    /**
     * @param {Array} notifications list of [channel, message] pairs
     */
    _onNotification: function (notifications) {
        var self = this;
        notifications.forEach(function (notif) {
            var message = notif[1];
            if (!message || !message._aidoo) {
                return; // not one of ours
            }
            var cb = self._handlers[message.session_id];
            if (cb) {
                cb(message);
            }
        });
    },

    register: function (sessionId, cb) {
        this._handlers[sessionId] = cb;
    },

    unregister: function (sessionId) {
        delete this._handlers[sessionId];
    },
});

core.serviceRegistry.add('aidoo', AidooService);

return AidooService;
});
