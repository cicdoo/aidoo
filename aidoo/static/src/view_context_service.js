/** @odoo-module **/
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import { registry } from "@web/core/registry";
import { proxy } from "@odoo/owl";

// Holds the context of the data view the user is currently looking at, fed by
// the form/list controller patches (see view_patches.js) and read reactively by
// the floating Aidoo widget. `current` is null when off a form/list view.
export const aidooViewContextService = {
    start() {
        const state = proxy({ current: null });
        return {
            state,
            set(ctx) {
                state.current = ctx;
            },
            clear() {
                state.current = null;
            },
        };
    },
};

registry.category("services").add("aidoo_view_context", aidooViewContextService);
