#!/usr/bin/env python3
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Standalone faster-whisper transcriber.

Runs under a dedicated venv (default /opt/odoo/whisper-venv), never imports
Odoo. Invoked by ``aidoo.session._run_whisper`` as a subprocess: writes the
transcript to --output, prints a one-line JSON status to stdout, and exits
nonzero on failure (stderr carries the reason). The --warmup flag downloads
and loads the model without transcribing, for one-time provisioning.
"""
import argparse
import json
import sys


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("audio", nargs="?")
    p.add_argument("--model", default="medium")
    p.add_argument("--model-dir", default=None)
    p.add_argument("--threads", type=int, default=8)
    p.add_argument("--language", default="",
                   help="ISO language code; empty = autodetect")
    p.add_argument("--output", default="", help="transcript file path")
    p.add_argument("--warmup", action="store_true",
                   help="download/load the model, then exit")
    a = p.parse_args()

    from faster_whisper import WhisperModel
    model = WhisperModel(a.model, device="cpu", compute_type="int8",
                         cpu_threads=a.threads, download_root=a.model_dir)
    if a.warmup:
        print(json.dumps({"ok": True, "warmup": True}))
        return 0
    if not a.audio or not a.output:
        print("audio path and --output are required", file=sys.stderr)
        return 2

    segments, info = model.transcribe(
        a.audio, language=(a.language or None), vad_filter=True, beam_size=5)
    # ``segments`` is a generator; decoding happens as it is consumed.
    text = "".join(s.text for s in segments).strip()
    if not text:
        text = "[The audio contained no recognizable speech.]"
    with open(a.output, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(json.dumps({"ok": True, "language": info.language,
                      "duration": round(info.duration or 0, 1)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
