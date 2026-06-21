odoo.define('aidoo.ReportFrame', function (require) {
"use strict";
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
var Widget = require('web.Widget');

// Renders generated HTML (e.g. a chart/report) inside a sandboxed iframe.
//
// Security: sandbox="allow-same-origin" WITHOUT allow-scripts. The HTML's own
// <script>s never run (it is untrusted markup), while staying same-origin lets
// us read its document to size the frame to its content. NEVER add allow-scripts
// here — allow-scripts + allow-same-origin together let the frame drop its own
// sandbox.
var ReportFrame = Widget.extend({
    template: 'aidoo.ReportFrame',

    /**
     * @param {Widget} parent
     * @param {Object} options {html}
     */
    init: function (parent, options) {
        this._super.apply(this, arguments);
        this.html = (options && options.html) || "";
    },

    start: function () {
        // The iframe "load" event does not bubble, so bind it directly.
        this.$('iframe').on('load', this._onLoad.bind(this));
        return this._super.apply(this, arguments);
    },

    _onLoad: function () {
        var el = this.$('iframe')[0];
        if (!el) {
            return;
        }
        // Default: the iframe flex-fills the panel height (see SCSS). Only grow it
        // beyond that — letting the panel body scroll — when the report content is
        // taller than the available height.
        el.style.height = "";
        var filled = el.offsetHeight;
        try {
            var doc = el.contentDocument;
            var contentH = doc && doc.body ? doc.body.scrollHeight : 0;
            if (contentH > filled) {
                el.style.height = contentH + "px";
            }
        } catch (e) {
            // Cross-origin read blocked (shouldn't happen with allow-same-origin);
            // the flex-filled height stands.
        }
    },
});

return ReportFrame;
});
