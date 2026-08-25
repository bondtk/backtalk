# backtalk: talk to your Claude Code agent out loud.
# Copyright (C) 2026 Jared Rhodenizer
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Proactive spoken reminders — Apple Reminders due right now, plus
ad-hoc verbal reminders queued mid-conversation (see
schedule_reminder.py: "remind me at 3 to take my medicine" becomes a
queued entry here). A background thread in main.py polls this on an
interval and speaks anything due straight through the mouth — no agent
turn, no permission ask, just talking, same as the "usage report"
console line does. Only fires while backtalk itself is running; there
is no wake-from-nothing here.
"""
import json
import subprocess
import time
from pathlib import Path

from backtalk.vlog import log

REPO = Path(__file__).resolve().parent.parent
VERBAL_QUEUE_PATH = REPO / "verbal_reminders.json"
_ANNOUNCED_PATH = REPO / ".apple_reminders_announced.json"

# A reminder counts as "due" from the moment it passes until this many
# seconds later — wide enough to survive a missed poll cycle or the
# machine waking from sleep, narrow enough that something overdue by
# days never suddenly announces itself the next time backtalk launches.
DUE_WINDOW_S = 600
POLL_INTERVAL_S = 30

# AppleScript date subtraction (due - now) yields plain seconds, so the
# comparison never depends on locale-specific date string parsing.
_APPLESCRIPT = '''
tell application "Reminders"
    set nowDate to current date
    set outStr to ""
    repeat with theList in lists
        repeat with r in (reminders of theList whose completed is false)
            set dd to due date of r
            if dd is not missing value then
                set deltaSec to (dd - nowDate)
                set outStr to outStr & (id of r) & tab & (name of r) & tab & deltaSec & linefeed
            end if
        end repeat
    end repeat
    return outStr
end tell
'''


def _load_announced():
    try:
        return json.loads(_ANNOUNCED_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_announced(data):
    try:
        _ANNOUNCED_PATH.write_text(json.dumps(data))
    except OSError as e:
        log(f"[scheduler] couldn't persist announced reminders: {e}")


def _due_apple_reminders():
    """Yields spoken text for each Apple Reminder that just came due."""
    try:
        r = subprocess.run(["osascript", "-e", _APPLESCRIPT],
                           capture_output=True, text=True, timeout=15)
    except Exception as e:
        log(f"[scheduler] Reminders query failed: {e}")
        return
    if r.returncode != 0:
        log(f"[scheduler] Reminders query error: {r.stderr.strip()[:200]}")
        return
    announced = _load_announced()
    now = time.time()
    dirty = False
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        rid, name, delta_raw = parts
        try:
            delta = float(delta_raw)
        except ValueError:
            continue
        due_epoch = now + delta
        age = now - due_epoch
        if rid in announced:
            continue
        if 0 <= age <= DUE_WINDOW_S:
            announced[rid] = due_epoch
            dirty = True
            yield f"Reminder, boss. {name}."
    if dirty:
        # prune entries once they've aged well past the due window, so
        # the file never grows without bound
        pruned = {k: v for k, v in announced.items()
                 if now - v <= DUE_WINDOW_S * 4}
        _save_announced(pruned)


def _due_verbal_reminders():
    """Yields spoken text for ad-hoc reminders queued mid-conversation
    (schedule_reminder.py) whose time has come, removing each from the
    queue as it fires so it never repeats."""
    try:
        items = json.loads(VERBAL_QUEUE_PATH.read_text())
    except (OSError, ValueError):
        return
    now = time.time()
    remaining, fired = [], []
    for item in items:
        due = item.get("due_epoch", 0)
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        if due <= now:
            fired.append(text)
        else:
            remaining.append(item)
    if len(remaining) != len(items):
        try:
            VERBAL_QUEUE_PATH.write_text(json.dumps(remaining, indent=2))
        except OSError as e:
            log(f"[scheduler] couldn't rewrite verbal reminder queue: {e}")
    for text in fired:
        yield f"Reminder, boss. {text}."


def check_and_announce(mouth):
    """One poll cycle: speak anything due, Apple Reminders and verbal
    alike. Safe to call on a timer — already-announced items never
    repeat, and a query failure just skips this cycle."""
    for line in _due_apple_reminders():
        log(f"[scheduler] {line}")
        mouth.say(line)
    for line in _due_verbal_reminders():
        log(f"[scheduler] {line}")
        mouth.say(line)
