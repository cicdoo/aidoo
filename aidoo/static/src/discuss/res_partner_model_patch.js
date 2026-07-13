/** @odoo-module **/
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import { ResPartner } from "@mail/core/common/res_partner_model";
import { fields } from "@mail/model/misc";

import { patch } from "@web/core/utils/patch";

// Client-side declaration of the synthetic flag the backend attaches to persona
// partners in the Discuss search store (see aidoo/models/res_partner.py). Without
// a declared field the mail store would drop the unknown key on insert.
patch(ResPartner.prototype, {
    setup() {
        super.setup();
        this.is_aidoo_persona = fields.Attr(false);
    },
});
