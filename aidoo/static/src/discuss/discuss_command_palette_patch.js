/** @odoo-module **/
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import { DiscussCommandPalette } from "@mail/discuss/core/public_web/discuss_command_palette";
import { cleanTerm } from "@mail/utils/common/format";

import { patch } from "@web/core/utils/patch";

// Odoo 19's "New message" (@) palette keeps only internal users
// (main_user_id.share === false) in buildResults(), which drops Aidoo personas
// (portal service users). The backend already injects them into the search store
// and tags them with `is_aidoo_persona`; re-add them here, reusing core's own
// makeDiscussCommand (its res.partner branch wires the openChat action).
patch(DiscussCommandPalette.prototype, {
    buildResults(filtered) {
        super.buildResults(...arguments);
        const shown = new Set(
            this.commands.map((c) => c.props?.persona?.id).filter(Boolean)
        );
        const personas = Object.values(this.store["res.partner"].records).filter(
            (p) =>
                p.is_aidoo_persona &&
                cleanTerm(p.displayName).includes(this.cleanedTerm) &&
                !shown.has(p.id)
        );
        for (const persona of personas) {
            this.commands.push(this.makeDiscussCommand(persona));
        }
    },
});
