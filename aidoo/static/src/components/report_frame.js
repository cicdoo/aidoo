/** @odoo-module **/
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import { Component, signal, t, useProps } from "@odoo/owl";

// Renders generated HTML (e.g. a chart/report) inside a sandboxed iframe,
// either inline (srcdoc, streaming/historical fallback) or from the server
// artifact URL (src) when the block was persisted as an ir.attachment.
//
// Security: sandbox="allow-same-origin" WITHOUT allow-scripts. The HTML's own
// <script>s never run (it is untrusted markup), while staying same-origin lets
// us read its document to size the frame to its content. NEVER add allow-scripts
// here — allow-scripts + allow-same-origin together let the frame drop its own
// sandbox. Server-served documents additionally carry a CSP "sandbox" header
// (opaque origin), which makes the height read below throw — they fall back to
// the CSS height.
export class ReportFrame extends Component {
    static template = "aidoo.ReportFrame";
    props = useProps({
        html: t.string().optional(),
        src: t.string().optional(),
    });

    frame = signal.ref();

    // srcdoc loads asynchronously, so resize on the iframe's load event.
    onLoad() {
        const el = this.frame();
        if (!el) {
            return;
        }
        try {
            const doc = el.contentDocument;
            const h = doc && doc.body ? doc.body.scrollHeight : 0;
            if (h) {
                el.style.height = h + "px";
            }
        } catch {
            // Opaque-origin document (CSP-sandboxed src) — the CSS fallback
            // height applies and the container scrolls instead.
        }
    }
}
