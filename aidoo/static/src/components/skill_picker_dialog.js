/** @odoo-module **/
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import { Component, proxy, signal, t, useProps, onMounted } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";

// A searchable multi-select picker for the conversation's instruction skills.
// Opened from the composer's skill button via the dialog service; clicking an
// entry toggles it, "Apply" calls `onSelect([ids])` and closes.
export class SkillPickerDialog extends Component {
    static template = "aidoo.SkillPickerDialog";
    static components = { Dialog };
    props = useProps({
        skills: t.array(),
        currentIds: t.array().optional(),
        onSelect: t.function(),
        close: t.function(),   // injected by the dialog service
    });

    searchRef = signal.ref();

    setup() {
        this.state = proxy({
            query: "",
            selected: new Set(this.props.currentIds || []),
        });
        onMounted(() => this.searchRef() && this.searchRef().focus());
    }

    get filtered() {
        const q = this.state.query.trim().toLowerCase();
        if (!q) {
            return this.props.skills;
        }
        return this.props.skills.filter(
            (s) =>
                (s.name || "").toLowerCase().includes(q) ||
                (s.description || "").toLowerCase().includes(q)
        );
    }

    toggle(id) {
        if (this.state.selected.has(id)) {
            this.state.selected.delete(id);
        } else {
            this.state.selected.add(id);
        }
    }

    clearAll() {
        this.state.selected.clear();
    }

    apply() {
        this.props.onSelect([...this.state.selected]);
        this.props.close();
    }
}
