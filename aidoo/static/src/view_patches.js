/** @odoo-module **/
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks";
import { onMounted, onPatched, onWillUnmount } from "@odoo/owl";
import { FormController } from "@web/views/form/form_controller";
import { ListController } from "@web/views/list/list_controller";

// Publish the current form/list view's context (model, record or filtered record
// set) to the aidoo_view_context service so the floating Aidoo widget can seed a
// conversation with what the user is looking at. Everything here is best-effort
// and fully guarded: a failure must never break a view or the web client.

function _useAidooFormContext() {
    let svc;
    try {
        svc = useService("aidoo_view_context");
    } catch {
        return;
    }
    const publish = () => {
        try {
            const root = this.model?.root;
            if (!root || !root.resModel) {
                return;
            }
            svc.set({ viewType: "form", resModel: root.resModel, resId: root.resId || null });
        } catch {
            // ignore
        }
    };
    onMounted(publish);
    onPatched(publish);
    onWillUnmount(() => {
        try {
            svc.clear();
        } catch {
            // ignore
        }
    });
}

function _useAidooListContext() {
    let svc;
    try {
        svc = useService("aidoo_view_context");
    } catch {
        return;
    }
    const publish = () => {
        try {
            const root = this.model?.root;
            if (!root || !root.resModel) {
                return;
            }
            const ids = (root.records || []).map((r) => r.resId).filter(Boolean);
            svc.set({
                viewType: "list",
                resModel: root.resModel,
                domain: root.domain || [],
                recordIds: ids,
                count: root.count,
            });
        } catch {
            // ignore
        }
    };
    onMounted(publish);
    onPatched(publish);
    onWillUnmount(() => {
        try {
            svc.clear();
        } catch {
            // ignore
        }
    });
}

patch(FormController.prototype, {
    setup() {
        super.setup();
        _useAidooFormContext.call(this);
    },
});

patch(ListController.prototype, {
    setup() {
        super.setup();
        _useAidooListContext.call(this);
    },
});
