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
"""Queue an ad-hoc spoken reminder for later in the same backtalk run.

The agent calls this mid-conversation when the person asks to be
reminded of something at a specific time ("remind me at 3 to take my
medicine"). The background scheduler (scheduler.py) picks the entry up
on its next poll and speaks it through the mouth when due — no agent
turn needed at fire time. It only fires while backtalk keeps running;
closing the voice line drops anything still queued.

Usage:
  python -m backtalk.schedule_reminder --at 15:00 --text "take your medicine"
  python -m backtalk.schedule_reminder --in-minutes 20 --text "check the oven"
"""
import argparse
import json
import time
from datetime import datetime, timedelta

from backtalk.scheduler import VERBAL_QUEUE_PATH


def _next_occurrence(hhmm: str) -> float:
    """Next wall-clock HH:MM (24h) — today if still ahead, else
    tomorrow. So "remind me at 3" said at 4 PM means tomorrow at 3, not
    an instant fire in the past."""
    hour, minute = (int(p) for p in hhmm.split(":"))
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.timestamp()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--at", metavar="HH:MM",
                   help="wall-clock time, 24h, today or tomorrow if "
                        "already past")
    g.add_argument("--in-minutes", type=float,
                   help="fire this many minutes from now")
    ap.add_argument("--text", required=True,
                    help="what to say out loud when it fires")
    args = ap.parse_args()

    due_epoch = (_next_occurrence(args.at) if args.at
                else time.time() + args.in_minutes * 60)

    try:
        items = json.loads(VERBAL_QUEUE_PATH.read_text())
    except (OSError, ValueError):
        items = []
    items.append({"text": args.text.strip(), "due_epoch": due_epoch})
    VERBAL_QUEUE_PATH.write_text(json.dumps(items, indent=2))

    when = datetime.fromtimestamp(due_epoch).strftime("%I:%M %p").lstrip("0")
    print(f'queued: "{args.text.strip()}" at {when}')


if __name__ == "__main__":
    main()
