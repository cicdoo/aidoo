/** @odoo-module **/
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import { Component, proxy, signal, t, useProps } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { SkillPickerDialog } from "./skill_picker_dialog";

export class AiComposer extends Component {
    static template = "aidoo.Composer";
    props = useProps({
        onSend: t.function(),
        disabled: t.boolean(),
        sessionId: t.number().optional(),
        // Skill picker (sits next to the attach button).
        skills: t.array().optional(),
        skillIds: t.array().optional(),
        onSelectSkills: t.function().optional(),
    });

    inputRef = signal.ref();
    fileRef = signal.ref();

    setup() {
        // files: [{ id, name, mimetype }] already uploaded and ready to attach.
        this.state = proxy({ text: "", files: [], uploading: false, error: "" });
        this.dialog = useService("dialog");
    }

    // Label for the picker button: the first selected skill's name, plus a
    // "+N" suffix when more skills are selected.
    get currentSkillName() {
        const ids = this.props.skillIds || [];
        const first = (this.props.skills || []).find((x) => ids.includes(x.id));
        if (!first) {
            return "";
        }
        const extra = ids.length - 1;
        return extra > 0 ? `${first.name} +${extra}` : first.name;
    }

    // Open the searchable skill dialog; applying a selection sets the chat's skills.
    openSkillPicker() {
        this.dialog.add(SkillPickerDialog, {
            skills: this.props.skills || [],
            currentIds: this.props.skillIds || [],
            onSelect: (ids) => this.props.onSelectSkills?.(ids),
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
        if (this.fileRef()) {
            this.fileRef().click();
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
        if (this.inputRef()) {
            this.inputRef().style.height = "auto";
        }
    }

    onInput(ev) {
        this.state.text = ev.target.value;
        this.autoGrow(ev);
    }

    autoGrow(ev) {
        const el = ev.target;
        el.style.height = "auto";
        el.style.height = Math.min(el.scrollHeight, 200) + "px";
    }
}
