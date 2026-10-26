# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
from odoo.tests.common import HttpCase, tagged


@tagged("post_install", "-at_install")
class TestAidooFrontend(HttpCase):
    """Smoke test: the web client starts with the Owl 3 frontend and the chat
    action opens. Odoo's browser runner fails the test on any JS console
    error, which is what catches things like `reactive is not a function`."""

    def test_chat_action_opens(self):
        self.browser_js(
            "/odoo",
            code="""
                (async () => {
                    const { Component } = odoo.loader.modules.get("@odoo/owl");
                    if (!Component) { throw new Error("Owl not loaded"); }
                    while (!odoo.__WOWL_DEBUG__?.root?.env?.services?.action) {
                        await new Promise((r) => setTimeout(r, 100));
                    }
                    const env = odoo.__WOWL_DEBUG__.root.env;
                    await env.services.action.doAction({
                        type: "ir.actions.client", tag: "aidoo.chat", name: "Aidoo",
                    });
                    const deadline = Date.now() + 10000;
                    while (!document.querySelector(".o_ai_chat") && Date.now() < deadline) {
                        await new Promise((r) => setTimeout(r, 100));
                    }
                    if (!document.querySelector(".o_ai_chat")) {
                        throw new Error("Aidoo chat action did not render");
                    }
                    console.log("test successful");
                })();
            """,
            login="admin",
        )
