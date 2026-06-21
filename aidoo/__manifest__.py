# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
{
    'name': "Aidoo",
    'summary': "Aidoo — chat with your Odoo, safely. One assistant, four AI providers "
               "(Claude, Codex, Gemini, Grok), permission-aware ORM tools and "
               "read-only SQL reporting.",
    'description': """
Aidoo — Unified AI Assistant for Odoo
=====================================
A chat interface (menu + OWL client action) that drives a headless AI CLI on the
server. The provider is chosen **per conversation** — Claude (Anthropic Claude
Code), Codex (OpenAI), Gemini (Google) or Grok (xAI) — behind one set of safe
tools exposed over a sandboxed MCP bridge:

* Every ORM action runs with the **current user's** permission level (never superuser).
* Raw SQL reporting is **read-only** (SELECT only) and gated to the *AI SQL Analyst* group.
* The CLI's own built-in tools (Bash, file write) are disabled; it can only act
  through the Odoo tool endpoints. Web access (WebFetch/WebSearch / web search) is
  an opt-in per-user grant, off by default.
* Replies stream into the chat in real time over the Odoo bus.

Authentication is per-user and per-provider (no shared subscription): each user
connects their own account from the chat — Claude via OAuth, Codex via device
code, Gemini/Grok by importing credentials or an API key — stored privately in a
per-user, per-provider config dir. Chatting is gated until the conversation's
provider is connected.

Requires the chosen provider's CLI installed on the server (claude / codex /
gemini / grok).
""",
    'author': "CICDoo",
    'website': "https://cicdoo.com",
    'category': 'Productivity/AI',
    'version': '13.0.1.0.0',
    'license': 'LGPL-3',
    'depends': ['web', 'bus', 'mail'],
    'external_dependencies': {'python': ['requests']},
    'data': [
        'security/aidoo_security.xml',
        'security/ir.model.access.csv',
        'data/aidoo_tool_data.xml',
        'data/aidoo_action.xml',
        'views/assets.xml',
        'views/res_config_settings_views.xml',
        'views/res_users_views.xml',
        'views/aidoo_log_views.xml',
        'views/aidoo_scheduled_views.xml',
    ],
    # Odoo 13 has no manifest 'assets' key. JS/SCSS are injected into
    # web.assets_backend via views/assets.xml; OWL-style QWeb templates are
    # loaded through the legacy 'qweb' key and rendered with qweb.render().
    'qweb': [
        'static/src/components/report_frame.xml',
        'static/src/components/message.xml',
        'static/src/components/message_list.xml',
        'static/src/components/composer.xml',
        'static/src/chat_action.xml',
        'static/src/floating_button.xml',
    ],
    'application': True,
    'installable': True,
}
