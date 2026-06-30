/** @odoo-module **/
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import { Component, useState, useRef } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { SkillPickerDialog } from "./skill_picker_dialog";

export class AiComposer extends Component {
    static template = "aidoo.Composer";
    static props = {
        onSend: Function,
        disabled: Boolean,
        sessionId: { type: Number, optional: true },
        // Skill picker (sits next to the attach button).
        skills: { type: Array, optional: true },
        skillId: { type: [Number, Boolean], optional: true },
        onSelectSkill: { type: Function, optional: true },
    };

    setup() {
        // files: [{ id, name, mimetype }] already uploaded and ready to attach.
        this.state = useState({ text: "", files: [], uploading: false, error: "" });
        this.inputRef = useRef("input");
        this.fileRef = useRef("file");
        this.dialog = useService("dialog");
    }

    // The currently-selected skill's name (for the picker button label/title).
    get currentSkillName() {
        const s = (this.props.skills || []).find((x) => x.id === this.props.skillId);
        return s ? s.name : "";
    }

    // Open the searchable skill dialog; selecting an entry applies it to the chat.
    openSkillPicker() {
        this.dialog.add(SkillPickerDialog, {
            skills: this.props.skills || [],
            currentId: this.props.skillId,
            onSelect: (id) => this.props.onSelectSkill?.(id),
        });
    }

    onKeydown(ev) {
        // Enter sends; Shift+Enter inserts a newline.
        if (ev.key === "Enter" && !ev.shiftKey) {
            ev.preventDefault();
            this._send();
        }
    }

    get canSend() {
        return (
            !this.props.disabled &&
            !this.state.uploading &&
            (!!this.state.text.trim() || this.state.files.length > 0)
        );
    }

    pickFiles() {
        if (this.fileRef.el) {
            this.fileRef.el.click();
        }
    }

    async onFilesChosen(ev) {
        const files = Array.from(ev.target.files || []);
        ev.target.value = ""; // allow re-picking the same file
        if (!files.length || !this.props.sessionId) {
            return;
        }
        this.state.error = "";
        this.state.uploading = true;
        try {
            const form = new FormData();
            form.append("session_id", this.props.sessionId);
            form.append("csrf_token", odoo.csrf_token);
            for (const f of files) {
                form.append("ufile", f);
            }
            const resp = await fetch("/aidoo/upload", {
                method: "POST",
                body: form,
            });
            const data = await resp.json();
            if (!resp.ok || data.error) {
                this.state.error = data.error || "Upload failed.";
                return;
            }
            this.state.files.push(...data.attachments);
        } catch {
            this.state.error = "Upload failed.";
        } finally {
            this.state.uploading = false;
        }
    }

    removeFile(id) {
        this.state.files = this.state.files.filter((f) => f.id !== id);
    }

    _send() {
        const text = this.state.text.trim();
        const files = this.state.files;
        if (!this.canSend) {
            return;
        }
        this.props.onSend(text, files);
        this.state.text = "";
        this.state.files = [];
        this.state.error = "";
        if (this.inputRef.el) {
            this.inputRef.el.style.height = "auto";
        }
    }

    autoGrow(ev) {
        const el = ev.target;
        el.style.height = "auto";
        el.style.height = Math.min(el.scrollHeight, 200) + "px";
    }
}
