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
queued entry here). Also proactively warns when Troy's Claude plan
usage (five-hour session window / weekly window) crosses 50/80/90%,
so he's never surprised by hitting a hard limit mid-task. Also checks,
once a week, whether backtalk's own upstream repo has new commits not
yet pulled into Troy's fork, logging the result straight to his vault.
A background thread in main.py polls this on an interval and speaks
anything due straight through the mouth — no agent turn, no permission
ask, just talking, same as the "usage report" console line does. Only
fires while backtalk itself is running; there is no wake-from-nothing
here.
"""
import json
import re
import subprocess
import time
from datetime import date
from pathlib import Path

from backtalk.config import CFG
from backtalk.vlog import log

REPO = Path(__file__).resolve().parent.parent
VERBAL_QUEUE_PATH = REPO / "verbal_reminders.json"
_ANNOUNCED_PATH = REPO / ".apple_reminders_announced.json"
_USAGE_WARNED_PATH = REPO / ".usage_warned.json"
_UPSTREAM_CHECK_PATH = REPO / ".upstream_check_state.json"
_UPSTREAM_VAULT_NOTE = Path(
    "/Users/troybond/Jarvis-Vault/04 - Personal/Proactive Voice Reminders.md")

# Weekday index (Monday=0 .. Sunday=6) this fires on — Tuesday, per Troy.
UPSTREAM_CHECK_WEEKDAY = 1

# Plan usage is checked far less often than the 30s reminder poll — it
# shells out to a fresh `claude -p` call, cheap but not free, so this
# throttles it independently within the same poll cycle.
USAGE_CHECK_INTERVAL_S = 900
USAGE_THRESHOLDS = (50, 80, 90)
_usage_last_check = 0.0

_USAGE_SESSION_RE = re.compile(
    r"Current session:\s*(\d+)%\s*used\s*\S\s*resets\s*([^\n(]+)")
_USAGE_WEEK_RE = re.compile(
    r"Current week[^:\n]*:\s*(\d+)%\s*used\s*\S\s*resets\s*([^\n(]+)")

# A reminder counts as "due" from the moment it passes until this many
# seconds later — wide enough to survive a missed poll cycle or the
# machine waking from sleep, narrow enough that something overdue by
# days never suddenly announces itself the next time backtalk launches.
DUE_WINDOW_S = 600
POLL_INTERVAL_S = 30

# AppleScript date subtraction (due - now) yields plain seconds, so the
# comparison never depends on locale-specific date string parsing.
#
# Fetches id/name/completed/due-date as four bulk lists per Reminders
# list, then filters in the loop, instead of `reminders of theList
# whose completed is false` — that "whose" filter checks the property
# on every reminder one at a time over Apple Events, a well-documented
# slow path in Reminders' scripting dictionary. Against Troy's 222
# reminders across 3 lists this still isn't fast (Reminders' own
# iCloud sync overhead dominates either way), so the real fix is a
# generous subprocess timeout (see _due_apple_reminders) rather than
# expecting this to be quick.
_APPLESCRIPT = '''
tell application "Reminders"
    set nowDate to current date
    set outStr to ""
    repeat with theList in lists
        set idList to id of reminders of theList
        set nameList to name of reminders of theList
        set completedList to completed of reminders of theList
        set dueList to due date of reminders of theList
        set n to count of idList
        repeat with i from 1 to n
            if not (item i of completedList) then
                set dd to item i of dueList
                if dd is not missing value then
                    set deltaSec to (dd - nowDate)
                    set outStr to outStr & (item i of idList) & tab & (item i of nameList) & tab & deltaSec & linefeed
                end if
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


def _ensure_reminders_running():
    """Launch Reminders.app if it's not already running. `open -a`
    launches faster and more reliably than letting the AppleScript
    trigger an implicit cold launch itself."""
    try:
        check = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to (name of processes) '
             'contains "Reminders"'],
            capture_output=True, text=True, timeout=5)
        if check.stdout.strip() == "true":
            return
    except Exception:
        pass
    subprocess.run(["open", "-a", "Reminders"], capture_output=True)


def _due_apple_reminders():
    """Yields spoken text for each Apple Reminder that just came due."""
    _ensure_reminders_running()
    try:
        # AppleScript's "whose completed is false" filter checks that
        # property on every reminder individually over Apple Events —
        # a known slow path in Reminders' scripting dictionary. Measured
        # 10-18s warm against Troy's 222 reminders across 3 lists; the
        # old 15s timeout was tripping on this legitimately-slow-but-
        # fine query (the "Reminders query failed" errors seen in the
        # terminal), not an actual fault. Generous margin here, not a
        # tighter fix, since this runs in a background thread and
        # doesn't block anything else.
        r = subprocess.run(["osascript", "-e", _APPLESCRIPT],
                           capture_output=True, text=True, timeout=45)
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


def _load_usage_warned():
    try:
        return json.loads(_USAGE_WARNED_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_usage_warned(data):
    try:
        _USAGE_WARNED_PATH.write_text(json.dumps(data))
    except OSError as e:
        log(f"[scheduler] couldn't persist usage warned state: {e}")


def _fetch_usage_text():
    try:
        r = subprocess.run(
            ["claude", "-p", "/usage", "--output-format", "text"],
            capture_output=True, text=True, timeout=30,
            cwd=CFG["agent_dir"],
        )
    except Exception as e:
        log(f"[scheduler] usage check failed: {e}")
        return None
    if r.returncode != 0:
        log(f"[scheduler] usage check error: {r.stderr.strip()[:200]}")
        return None
    return r.stdout


def _due_usage_warnings():
    """Yields a spoken warning whenever Troy's five-hour or weekly plan
    usage crosses 50/80/90% for the first time in the current window.
    Throttled to USAGE_CHECK_INTERVAL_S regardless of poll frequency."""
    global _usage_last_check
    now = time.time()
    if now - _usage_last_check < USAGE_CHECK_INTERVAL_S:
        return
    _usage_last_check = now
    text = _fetch_usage_text()
    if not text:
        return
    warned = _load_usage_warned()
    dirty = False
    for key, label, pattern in (
        ("session", "five-hour", _USAGE_SESSION_RE),
        ("week", "weekly", _USAGE_WEEK_RE),
    ):
        m = pattern.search(text)
        if not m:
            continue
        pct = int(m.group(1))
        state = warned.get(key, {"last_threshold": 0})
        if pct < state.get("last_threshold", 0):
            # usage is lower than the last threshold we warned about —
            # the window rolled over, so thresholds fire again. (Not
            # keying off the CLI's displayed reset time: it's rounded
            # to the minute and jitters +/-1 minute between calls,
            # which would falsely look like a new window every time.)
            state = {"last_threshold": 0}
        crossed = [t for t in USAGE_THRESHOLDS
                   if pct >= t > state.get("last_threshold", 0)]
        if crossed:
            top = max(crossed)
            state["last_threshold"] = top
            warned[key] = state
            dirty = True
            yield (f"Heads up, boss — your {label} Claude usage just "
                   f"crossed {top} percent.")
        elif state != warned.get(key):
            warned[key] = state
            dirty = True
    if dirty:
        _save_usage_warned(warned)


def _load_upstream_state():
    try:
        return json.loads(_UPSTREAM_CHECK_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_upstream_state(data):
    try:
        _UPSTREAM_CHECK_PATH.write_text(json.dumps(data))
    except OSError as e:
        log(f"[scheduler] couldn't persist upstream-check state: {e}")


def _append_upstream_note(body):
    try:
        with _UPSTREAM_VAULT_NOTE.open("a") as f:
            f.write(f"\n## Upstream check — {date.today().isoformat()}\n{body}\n")
    except OSError as e:
        log(f"[scheduler] couldn't write upstream-check note: {e}")


def _due_upstream_check():
    """Once a week (UPSTREAM_CHECK_WEEKDAY), checks backtalk's upstream
    remote (jaredrhod/backtalk) for commits not yet in this fork's main
    branch. Every other poll cycle this is a single cheap date/state
    comparison — no subprocess, no network call — so it costs nothing
    to run on the normal 30s cadence. The actual git fetch only happens
    once, on the one due day, then marks itself done for the ISO week
    so it won't check again until next week even if polled thousands
    more times. Read-only: never merges or pulls, just reports."""
    today = date.today()
    if today.weekday() != UPSTREAM_CHECK_WEEKDAY:
        return
    week_key = list(today.isocalendar()[:2])  # [year, week] — JSON-stable
    state = _load_upstream_state()
    if state.get("last_checked_week") == week_key:
        return
    try:
        fetch = subprocess.run(["git", "fetch", "upstream"], cwd=REPO,
                               capture_output=True, text=True, timeout=30)
        if fetch.returncode != 0:
            log(f"[scheduler] upstream fetch failed: {fetch.stderr.strip()[:200]}")
            return
        log_r = subprocess.run(
            ["git", "log", "--oneline", "HEAD..upstream/main"], cwd=REPO,
            capture_output=True, text=True, timeout=15)
    except Exception as e:
        log(f"[scheduler] upstream check failed: {e}")
        return
    if log_r.returncode != 0:
        log(f"[scheduler] upstream log failed: {log_r.stderr.strip()[:200]}")
        return
    commits = [ln for ln in log_r.stdout.splitlines() if ln.strip()]
    state["last_checked_week"] = week_key
    _save_upstream_state(state)
    if commits:
        _append_upstream_note(
            f"{len(commits)} new commit(s) on jaredrhod/backtalk not yet "
            f"merged:\n" + "\n".join(f"- {c}" for c in commits))
        yield (f"Backtalk upstream check, boss — {len(commits)} new "
               f"commit{'s' if len(commits) != 1 else ''} waiting on the "
               f"original repo, logged in your vault.")
    else:
        _append_upstream_note("Nothing new — fork is caught up.")
        yield "Backtalk upstream check, boss — nothing new this week, you're caught up."


def check_and_announce(mouth):
    """One poll cycle: speak anything due, Apple Reminders and verbal
    alike, plus any plan-usage threshold just crossed. Safe to call on
    a timer — already-announced items never repeat, and a query
    failure just skips this cycle."""
    for line in _due_apple_reminders():
        log(f"[scheduler] {line}")
        mouth.say(line)
    for line in _due_verbal_reminders():
        log(f"[scheduler] {line}")
        mouth.say(line)
    for line in _due_usage_warnings():
        log(f"[scheduler] {line}")
        mouth.say(line)
    for line in _due_upstream_check():
        log(f"[scheduler] {line}")
        mouth.say(line)
