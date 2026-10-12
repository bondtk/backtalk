# backtalk: talk to your Claude Code agent out loud.
# Copyright (C) 2026 Jared Rhodenizer
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The dashboard inbox: the Jarvis dashboard's buttons, as typed lines.

The dashboard appends one JSON object per line to .voice_inbox in
signals_dir. This thread takes the file, drops anything older than
MAX_AGE_S (a click from before a restart must never fire later), asks
main.py's accept() whether that item is allowed RIGHT NOW, and puts the
approved text on the same queue typed terminal lines use. Nothing here
decides what a command means; the normal path does, exactly as if it
were typed or spoken. Every failure is logged and swallowed.
"""
import json
import os
import threading
import time

from backtalk import signals
from backtalk.config import CFG
from backtalk.vlog import log

INBOX = os.path.join(CFG["signals_dir"], ".voice_inbox")
MAX_AGE_S = 15
SAY_MAX = 8000          # characters in one typed message from the dashboard
POLL_S = 0.3
PROMPTS = {
    "look_me": "Take a webcam look at me and tell me what you see.",
    "look_screen": "Take a screenshot of my screen and tell me what you see.",
}


def start(typed_q, stop: threading.Event, accept):
    def _loop():
        signals.set_caps(inbox=True, events=True, say=True)
        while not stop.is_set():
            try:
                if os.path.exists(INBOX):
                    taken = INBOX + ".taking"
                    os.replace(INBOX, taken)       # atomic: no half-read lines
                    with open(taken) as f:
                        lines = f.read().splitlines()
                    os.remove(taken)
                    for ln in lines:
                        try:
                            item = json.loads(ln)
                        except ValueError:
                            continue
                        if time.time() - float(item.get("ts", 0)) > MAX_AGE_S:
                            log("[dash] dropped a stale dashboard command")
                            continue
                        text = accept(item)
                        if text:
                            shown = (f"typed message ({len(text)} chars)"
                                     if item.get("kind") == "say" else text)
                            log(f"[dash] {item.get('kind')}: {shown}")
                            typed_q.put(text)
                        else:
                            log(f"[dash] refused {item.get('kind')!r} (not allowed now)")
            except Exception as e:
                log(f"[dash] inbox error: {e}")
            stop.wait(POLL_S)

    threading.Thread(target=_loop, daemon=True).start()
