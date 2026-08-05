# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "aidoo")
class TestConfigPersistence(TransactionCase):

    def test_admin_editable_seeds_are_noupdate(self):
        """Settings the admin can change via Settings -> Aidoo (or Technical ->
        Scheduled Actions) must not be reloaded from XML on every module
        upgrade, or a customized Odoo Host URL / SQL toggle / provider /
        scheduler interval silently reverts and breaks sending."""
        IMD = self.env["ir.model.data"]
        for name in (
            "param_base_url", "param_sql_enabled", "param_default_provider",
            "cron_ai_reaper", "cron_ai_scheduled",
        ):
            rec = IMD.search([("module", "=", "aidoo"), ("name", "=", name)])
            self.assertTrue(rec, "missing ir.model.data for aidoo.%s" % name)
            self.assertTrue(
                rec.noupdate,
                "aidoo.%s must be noupdate so upgrades don't reset it" % name)
