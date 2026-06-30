/** @odoo-module **/
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks";
import { onMounted, onPatched, onWillUnmount } from "@odoo/owl";
import { FormController } from "@web/views/form/form_controller";
import { ListController } from "@web/views/list/list_controller";
import { KanbanController } from "@web/views/kanban/kanban_controller";
import { PivotController } from "@web/views/pivot/pivot_controller";
import { GraphController } from "@web/views/graph/graph_controller";
import { CalendarController } from "@web/views/calendar/calendar_controller";

// Publish the current view's context (model, focused record, or — on any
// multi-record view — the active filters/grouping/domain and the user's record
// selection) to the aidoo_view_context service, so the floating Aidoo widget can
// seed a conversation with exactly what the user is looking at. Everything here
// is best-effort and fully guarded: a failure must never break a view.

// A readable one-line summary of the active search facets (the chips shown in
// the search bar), e.g. "Customer · Order Date: 2024 > Month".
function _facetSummary(searchModel) {
    try {
        const facets = searchModel.facets || [];
        const chips = facets
            .map((f) => {
                const sep = f.separator ? ` ${f.separator} ` : ", ";
                const vals = (f.values || []).join(sep);
                return f.title ? `${f.title}: ${vals}` : vals;
            })
            .filter(Boolean);
        return chips.join(" · ");
    } catch {
        return "";
    }
}

function _useAidooViewContext(viewType) {
    let svc;
    try {
        svc = useService("aidoo_view_context");
    } catch {
        return;
    }
    const publish = () => {
        try {
            const sm = this.env.searchModel;
            const resModel = sm?.resModel || this.model?.root?.resModel;
            if (!resModel) {
                return;
            }
            const ctx = {
                viewType,
                resModel,
                domain: (sm && sm.domain) || this.model?.root?.domain || [],
                groupby: (sm && sm.groupBy) || [],
                filters: sm ? _facetSummary(sm) : "",
            };
            // Record selection (list / kanban / calendar only — pivot/graph have
            // no record selection, so this stays empty there).
            const root = this.model?.root;
            const selection = root && root.selection;
            if (selection && selection.length) {
                ctx.selectedIds = selection.map((r) => r.resId).filter(Boolean);
                ctx.selectAll = !!root.isDomainSelected;
            }
            if (root && typeof root.count === "number") {
                ctx.count = root.count;
            }
            svc.set(ctx);
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

patch(FormController.prototype, {
    setup() {
        super.setup();
        _useAidooFormContext.call(this);
    },
});

const _MULTI = [
    [ListController, "list"],
    [KanbanController, "kanban"],
    [PivotController, "pivot"],
    [GraphController, "graph"],
    [CalendarController, "calendar"],
];
for (const [Controller, viewType] of _MULTI) {
    patch(Controller.prototype, {
        setup() {
            super.setup();
            _useAidooViewContext.call(this, viewType);
        },
    });
}
