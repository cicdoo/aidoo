# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
from pathlib import Path

from odoo.modules import get_manifest
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestAidooAssets(TransactionCase):
    def test_frontend_files_are_bundled(self):
        """Every static/src JS/XML/SCSS file must be listed in the manifest,
        otherwise an import of it breaks the whole bundle at runtime."""
        manifest = get_manifest("aidoo")
        listed = set(manifest["assets"]["web.assets_backend"])
        root = Path(__file__).resolve().parent.parent
        found = {
            "aidoo/" + p.relative_to(root).as_posix()
            for ext in ("js", "xml", "scss")
            for p in (root / "static" / "src").rglob("*." + ext)
        }
        self.assertFalse(found - listed, "Frontend files missing from manifest")
