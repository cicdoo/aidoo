# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).


def migrate(cr, version):
    """The five records below (3 ir.config_parameter + 2 ir.cron) used to be
    non-noupdate, so every module upgrade silently reset the admin's Odoo Host
    URL / SQL toggle / default provider / scheduler interval back to the
    hardcoded seed values. Flip noupdate on now that the XML block is marked
    noupdate="1", so this stops happening on every upgrade from here on.
    Values already in the DB (whatever they currently are) are left as-is —
    there is no way to recover what an admin may have set before a prior
    upgrade clobbered it.
    """
    cr.execute("""
        UPDATE ir_model_data SET noupdate = true
        WHERE module = 'aidoo' AND name IN (
            'param_base_url', 'param_sql_enabled', 'param_default_provider',
            'cron_ai_reaper', 'cron_ai_scheduled')
    """)
