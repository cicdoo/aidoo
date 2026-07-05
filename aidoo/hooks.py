# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Install/upgrade hooks."""


def post_init_hook(env):
    """Seed the built-in default persona (adopts the legacy @aidoo identity) so
    ``@aidoo`` works immediately after install/upgrade, and make sure every
    persona service account is a portal (not internal) user."""
    env["aidoo.persona"]._ensure_default_persona()
    env["aidoo.persona"]._demote_bot_users_to_portal()
