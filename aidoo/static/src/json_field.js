odoo.define('aidoo.json_field', function (require) {
"use strict";

// Read-only viewer for the custom ``json`` field type (Odoo 13 has no json
// widget). The server sends pretty-printed JSON text via convert_to_read, so we
// just render it in a <pre>. Registered both for the field type and an explicit
// widget name so views can opt in with widget="aidoo_json".
var AbstractField = require('web.AbstractField');
var fieldRegistry = require('web.field_registry');

var JsonField = AbstractField.extend({
    className: 'o_field_json',
    tagName: 'pre',
    supportedFieldTypes: ['json'],

    _render: function () {
        var val = this.value;
        var text = '';
        if (val !== false && val !== null && val !== undefined) {
            if (typeof val === 'string') {
                text = val;
            } else {
                try {
                    text = JSON.stringify(val, null, 2);
                } catch (e) {
                    text = String(val);
                }
            }
        }
        this.$el.text(text);
    },
});

fieldRegistry.add('json', JsonField);
fieldRegistry.add('aidoo_json', JsonField);

return JsonField;
});
