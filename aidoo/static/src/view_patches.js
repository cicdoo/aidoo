/** @odoo-module **/
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import { patch } from "@web/core/utils/patch";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { onMounted, onPatched, onWillUnmount } from "@odoo/owl";

// Publish the current view's context (model, focused record, or — on any
// multi-record view — the active filters/grouping/domain and the user's record
// selection) to the aidoo_view_context service, so the floating Aidoo widget can
// seed a conversation with exactly what the user is looking at. Everything here
// is best-effort and fully guarded: a failure must never break a view.
//
// We resolve the view controllers from the views REGISTRY rather than importing
// them — pivot/graph (and other) controllers live in lazy-loaded bundles, so a
// static import would break the eager asset bundle. We patch each controller as
// it registers (lazy views register on first open, firing the registry UPDATE).

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

// viewType -> the setup hook to splice into that view's controller.
const VIEW_HOOKS = {
    form() {
        _useAidooFormContext.call(this);
    },
    list() {
        _useAidooViewContext.call(this, "list");
    },
    kanban() {
        _useAidooViewContext.call(this, "kanban");
    },
    pivot() {
        _useAidooViewContext.call(this, "pivot");
    },
    graph() {
        _useAidooViewContext.call(this, "graph");
    },
    calendar() {
        _useAidooViewContext.call(this, "calendar");
    },
};

const views = registry.category("views");
const _patched = new WeakSet();

function _tryPatch(viewType) {
    const entry = views.get(viewType, null);
    const Controller = entry && entry.Controller;
    if (!Controller || _patched.has(Controller)) {
        return;
    }
    _patched.add(Controller);
    const hook = VIEW_HOOKS[viewType];
    patch(Controller.prototype, {
        setup() {
            super.setup();
            try {
                hook.call(this);
            } catch {
                // never let context capture break a view
            }
        },
    });
}

function _patchAll() {
    for (const viewType of Object.keys(VIEW_HOOKS)) {
        try {
            _tryPatch(viewType);
        } catch {
            // ignore
        }
    }
}

// Patch the views already registered, and any that register later (lazy views
// like pivot/graph register the first time the user opens them).
_patchAll();
views.addEventListener("UPDATE", _patchAll);
