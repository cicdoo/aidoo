odoo.define('aidoo.cog_menu_item', function (require) {
"use strict";
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).

// "Ask Aidoo" entry in the form-view Action (cog) menu — the Odoo 13 Sidebar.
// Opens the Aidoo chat seeded with the record the user is currently viewing.
// v18's cogMenu registry does not exist here, so we inject a Sidebar item.
var FormController = require('web.FormController');
var session = require('web.session');
var core = require('web.core');
var _t = core._t;

// Resolved once; the item is only added for users who may use the assistant.
var _allowed = false;
session.user_has_group('aidoo.group_ai_user').then(function (r) {
    _allowed = r;
});

FormController.include({
    renderSidebar: function () {
        var self = this;
        return Promise.resolve(this._super.apply(this, arguments)).then(function (res) {
            if (_allowed && self.sidebar) {
                self.sidebar._addItems('other', [{
                    label: _t("Ask Aidoo"),
                    callback: self._aidooAsk.bind(self),
                }]);
                self.sidebar._redraw();
            }
            return res;
        });
    },

    _aidooAsk: function () {
        var params = {};
        try {
            var st = this.model.get(this.handle, { raw: true });
            if (st && st.model && st.res_id) {
                params.context_model = st.model;
                params.context_res_id = st.res_id;
            }
        } catch (e) {
            // open a plain chat if the record context can't be read
        }
        this.do_action({
            type: "ir.actions.client",
            tag: "aidoo.chat",
            name: _t("Aidoo"),
            params: params,
        });
    },
});
});
