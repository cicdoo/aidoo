/** @odoo-module **/
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import { Component } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { user } from "@web/core/user";
import { DropdownItem } from "@web/core/dropdown/dropdown_item";
import { STATIC_ACTIONS_GROUP_NUMBER } from "@web/search/action_menus/action_menus";
import { _t } from "@web/core/l10n/translation";

const cogMenuRegistry = registry.category("cogMenu");

// "Ask Aidoo" entry in the form-view Actions (cog) menu. Opens the Aidoo chat
// seeded with the record the user is currently looking at.
export class AskAidooMenu extends Component {
    static template = "aidoo.AskAidooMenu";
    static components = { DropdownItem };
    static props = {};

    setup() {
        this.action = useService("action");
    }

    askAidoo() {
        const controller = this.action.currentController;
        const params = {};
        // Seed the focus record when we're on a saved record; otherwise just
        // open a plain chat.
        if (controller?.props?.resModel && controller?.props?.resId) {
            params.context_model = controller.props.resModel;
            params.context_res_id = controller.props.resId;
        }
        this.action.doAction({
            type: "ir.actions.client",
            tag: "aidoo.chat",
            name: _t("Aidoo"),
            params,
        });
    }
}

export const askAidooMenuItem = {
    Component: AskAidooMenu,
    groupNumber: STATIC_ACTIONS_GROUP_NUMBER,
    // On form views, for users who may use the assistant.
    isDisplayed: async (env) =>
        env.config.viewType === "form" &&
        (await user.hasGroup("aidoo.group_ai_user")),
};

cogMenuRegistry.add("aidoo-ask", askAidooMenuItem, { sequence: 15 });
