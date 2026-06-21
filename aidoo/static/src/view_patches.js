odoo.define('aidoo.view_patches', function (require) {
"use strict";
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).

// Publish the current form/list view's context (model, record or filtered record
// set) to the aidoo_view_context service so the floating Aidoo widget can seed a
// conversation with what the user is looking at. Everything here is best-effort
// and fully guarded: a failure must never break a view or the web client.
var FormController = require('web.FormController');
var ListController = require('web.ListController');

function _publishForm() {
    try {
        var st = this.model.get(this.handle, { raw: true });
        if (!st || !st.model) {
            return;
        }
        this.call('aidoo_view_context', 'set', {
            viewType: "form",
            resModel: st.model,
            resId: st.res_id || null,
        });
    } catch (e) {
        // ignore — never break the view
    }
}

function _publishList() {
    try {
        var st = this.model.get(this.handle);
        if (!st || !st.model) {
            return;
        }
        var domain = [];
        try {
            domain = st.getDomain ? st.getDomain() : (st.domain || []);
        } catch (e) {
            domain = [];
        }
        this.call('aidoo_view_context', 'set', {
            viewType: "list",
            resModel: st.model,
            domain: domain,
            recordIds: st.res_ids || [],
        });
    } catch (e) {
        // ignore
    }
}

function _clearCtx() {
    try {
        this.call('aidoo_view_context', 'clear');
    } catch (e) {
        // ignore
    }
}

FormController.include({
    _update: function () {
        var self = this;
        return Promise.resolve(this._super.apply(this, arguments)).then(function (res) {
            _publishForm.call(self);
            return res;
        });
    },
    destroy: function () {
        _clearCtx.call(this);
        this._super.apply(this, arguments);
    },
});

ListController.include({
    _update: function () {
        var self = this;
        return Promise.resolve(this._super.apply(this, arguments)).then(function (res) {
            _publishList.call(self);
            return res;
        });
    },
    destroy: function () {
        _clearCtx.call(this);
        this._super.apply(this, arguments);
    },
});
});
