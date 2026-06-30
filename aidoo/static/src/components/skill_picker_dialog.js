/** @odoo-module **/
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import { Component, useState, useRef, onMounted } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";

// A searchable picker for the conversation's instruction skill. Opened from the
// composer's skill button via the dialog service; calls `onSelect(id|false)`
// and closes when the user picks an entry.
export class SkillPickerDialog extends Component {
    static template = "aidoo.SkillPickerDialog";
    static components = { Dialog };
    static props = {
        skills: { type: Array },
        currentId: { type: [Number, Boolean], optional: true },
        onSelect: { type: Function },
        close: { type: Function },   // injected by the dialog service
    };

    setup() {
        this.state = useState({ query: "" });
        this.searchRef = useRef("search");
        onMounted(() => this.searchRef.el && this.searchRef.el.focus());
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

    pick(id) {
        this.props.onSelect(id || false);
        this.props.close();
    }
}
