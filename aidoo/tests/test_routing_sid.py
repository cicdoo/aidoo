# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
import os

from odoo.http.session import session_store
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestRoutingSid(TransactionCase):
    def test_mint_and_delete_routing_sid(self):
        Session = self.env["aidoo.session"]
        db = self.env.cr.dbname
        sid = Session._mint_routing_sid(db)
        self.assertTrue(sid)
        store = session_store()
        self.assertEqual(store.get(sid).db, db)
        Session._delete_routing_sid(sid)
        self.assertFalse(os.path.exists(store.get_session_path(sid)))
