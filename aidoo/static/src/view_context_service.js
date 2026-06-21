odoo.define('aidoo.view_context_service', function (require) {
"use strict";
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).

// Holds the context of the data view the user is currently looking at, fed by the
// form/list controller patches (see view_patches.js) and read by the floating
// Aidoo widget. ``current`` is null when off a form/list view. Odoo 13 has no OWL
// reactive(); instead we emit ``aidoo_view_context:changed`` on core.bus so the
// floating widget can refresh.
var AbstractService = require('web.AbstractService');
var core = require('web.core');

var AidooViewContext = AbstractService.extend({
    start: function () {
        this._super.apply(this, arguments);
        this.current = null;
    },

    set: function (ctx) {
        this.current = ctx;
        core.bus.trigger('aidoo_view_context:changed', ctx);
    },

    clear: function () {
        this.current = null;
        core.bus.trigger('aidoo_view_context:changed', null);
    },

    get: function () {
        return this.current;
    },
});

core.serviceRegistry.add('aidoo_view_context', AidooViewContext);

return AidooViewContext;
});
