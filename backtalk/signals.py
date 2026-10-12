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
"""The signal bus — tiny files any other program can watch.

The voice line leaves notes; faces read the notes. That one dumb trick
is the whole integration surface:

  .voice_state        idle | listening | thinking | speaking
  .voice_waveform     JSON {ts, samples: [64 floats]} while audio plays
  .voice_loading_pid  exists while the thinking sound is playing
  .voice_rate_limits  JSON {window: {utilization, resets_at}} — only
                      written when show_usage is on

Written to signals_dir (default: the repo root). Visualizers built on
this contract just work.

THE BAREHANDS SEAM: set barehands_state_dir in backtalk.json to a
barehands checkout's state/ folder and the same signals are mirrored in
its format (state/state as a bare word, state/wave.json normalized
0..1) — the on-screen ring becomes your agent's face with zero glue.

Every write is wrapped: the bus must never crash the voice line.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time

import numpy as np

from backtalk.config import CFG

_DIR = CFG["signals_dir"]
_STATE_FILE = os.path.join(_DIR, ".voice_state")
_WAVEFORM_FILE = os.path.join(_DIR, ".voice_waveform")
_LOADING_PID_FILE = os.path.join(_DIR, ".voice_loading_pid")
_DIRECTION_FILE = os.path.join(_DIR, ".voice_direction")
_REPLY_DONE_FILE = os.path.join(_DIR, ".voice_reply_done")
_RATE_LIMIT_FILE = os.path.join(_DIR, ".voice_rate_limits")

_BH = CFG.get("barehands_state_dir") or ""
_BH_STATE = os.path.join(_BH, "state") if _BH else ""
_BH_WAVE = os.path.join(_BH, "wave.json") if _BH else ""

_THINKING_SOUND = CFG.get("thinking_sound") or ""

_WAVEFORM_MIN_INTERVAL = 1.0 / 15   # ~15 writes/sec is plenty for 60fps reads
_last_waveform_write = 0.0
_static_proc: subprocess.Popen | None = None


_cur_state: str | None = None
_think_token = 0
_TERMINAL_STATUS = CFG.get("terminal_status", True)
_HEARTBEAT_SECONDS = 15


def _heartbeat(token: int, started: float):
    next_beat = _HEARTBEAT_SECONDS
    while True:
        time.sleep(1.0)
        if _think_token != token or _cur_state != "thinking":
            return
        elapsed = int(time.time() - started)
        if elapsed >= next_beat:
            print(f"(still working, {elapsed}s)", flush=True)
            next_beat = elapsed + _HEARTBEAT_SECONDS


def _announce(prior: str | None, name: str):
    """Print a one-line terminal status on state changes. Never raises."""
    global _think_token
    try:
        if name == "thinking":
            _think_token += 1
            print("(thinking...)", flush=True)
            threading.Thread(target=_heartbeat,
                             args=(_think_token, time.time()),
                             daemon=True).start()
        elif name == "idle" and prior in ("thinking", "speaking"):
            print("(done, your turn)", flush=True)
        elif name == "listening":
            print("(listening...)", flush=True)
    except Exception:
        pass


def set_state(name: str):
    """Write the state. Never raises — the show must go on."""
    global _cur_state
    prior, _cur_state = _cur_state, name
    if _TERMINAL_STATUS and name != prior:
        _announce(prior, name)
    try:
        with open(_STATE_FILE, "w") as f:
            f.write(name)
    except OSError:
        pass
    if _BH_STATE:
        try:
            with open(_BH_STATE, "w") as f:
                f.write(name)
        except OSError:
            pass


def feed_waveform(pcm: np.ndarray):
    """Feed one PCM block (int16) — throttled, downsampled to 64 points.

    Also re-asserts state="speaking" on the same throttle: this only runs
    while the mouth is audibly playing, so the bus self-heals within
    ~70ms if a stray writer stomps the state mid-speech. (That self-heal
    rule once closed a bug that took a whole evening to find.)"""
    global _last_waveform_write
    if pcm.size == 0:
        return
    now = time.time()
    if now - _last_waveform_write < _WAVEFORM_MIN_INTERVAL:
        return
    _last_waveform_write = now
    try:
        idx = np.linspace(0, pcm.size - 1, 64).astype(int)
        raw = pcm[idx].astype(float)
        with open(_WAVEFORM_FILE, "w") as f:
            f.write(json.dumps({"ts": now, "samples": raw.tolist()}))
        if _BH_WAVE:
            norm = np.clip(np.abs(raw) / 32768.0, 0.0, 1.0)
            with open(_BH_WAVE, "w") as f:
                f.write(json.dumps({"ts": now, "samples": norm.tolist()}))
    except (OSError, ValueError):
        pass
    set_state("speaking")


def direction(items):
    """Stage directions the agent wrote into its reply, published at the
    moment the audio carrying them starts playing.

    Your agent can emit `<<anything>>` inline and backtalk will never speak
    it. What the tag MEANS is deliberately not backtalk's business: it
    publishes the raw strings and something else decides. That is the whole
    reason this is a file and not a plugin API.

    The timing is the point, and it is the one part a watcher cannot do for
    itself: these fire when the sentence becomes AUDIBLE, not when the model
    generated it. A screen cue lands on the spoken word instead of seconds
    early. Never raises."""
    if not items:
        return
    try:
        with open(_DIRECTION_FILE, "w") as f:
            f.write(json.dumps({"ts": time.time(), "directions": list(items)}))
    except OSError:
        pass


def reply_done():
    """One reply has finished speaking and its audio has fully drained.

    Distinct from the state going idle, which also happens in the gaps
    BETWEEN sentences of the same reply. Anything waiting for the agent to
    genuinely stop talking wants this rather than a state flicker. Never
    raises."""
    try:
        with open(_REPLY_DONE_FILE, "w") as f:
            f.write(json.dumps({"ts": time.time()}))
    except OSError:
        pass


_rate_limits: dict = {}


def set_model(model_id: str):
    """Which model is answering right now, for a face to show under the
    usage windows. Rides the same file as the usage readout so a face
    reads one payload, not two. A model name is not spend, so unlike
    set_rate_limit this is written regardless of show_usage.

    Stored pretty ("claude-opus-5-5" -> "Opus 5.5") so every face shows
    the same words. Never raises."""
    try:
        parts = str(model_id).replace("claude-", "").split("-")
        name = parts[0].capitalize()
        ver = ".".join(p for p in parts[1:] if p.isdigit() and len(p) <= 2)
        _rate_limits["model"] = f"{name} {ver}".strip()
        with open(_RATE_LIMIT_FILE, "w") as f:
            f.write(json.dumps(_rate_limits))
    except Exception:
        pass


def set_rate_limit(window: str, utilization, resets_at):
    """One usage window's reading — how much of the plan is spent.

    Merged rather than replaced, because the reading arrives one window
    at a time and a face wants to draw both at once. `utilization` is a
    0..1 fraction (or None when the window has not reported a number
    yet, which is a real state and not an error); `resets_at` is a unix
    epoch.

    NOTHING CALLS THIS UNLESS show_usage IS ON. That is a privacy
    default, not a performance one: this is the account holder's own
    spend, and it renders on a face that may well be pointed at a
    camera. It never appears without being asked for. (Community fix,
    ai-visualizer issue #1.)

    Never raises."""
    if not window:
        return
    _rate_limits[window] = {"utilization": utilization,
                            "resets_at": resets_at}
    try:
        with open(_RATE_LIMIT_FILE, "w") as f:
            f.write(json.dumps(_rate_limits))
    except OSError:
        pass


def _player_cmd(path: str) -> list[str] | None:
    if sys.platform == "darwin":
        return ["afplay", "-v", "0.35", path]
    for cand in ("ffplay", "aplay", "paplay"):
        from shutil import which
        if which(cand):
            if cand == "ffplay":
                return ["ffplay", "-nodisp", "-autoexit", "-loglevel",
                        "quiet", "-volume", "35", path]
            return [cand, path]
    return None


def static_start():
    """Optional thinking sound — plays while the brain works."""
    global _static_proc
    if not _THINKING_SOUND or not os.path.exists(_THINKING_SOUND):
        return
    static_stop()
    cmd = _player_cmd(_THINKING_SOUND)
    if not cmd:
        return
    try:
        _static_proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(_LOADING_PID_FILE, "w") as f:
            f.write(str(_static_proc.pid))
    except OSError:
        _static_proc = None


def static_stop():
    global _static_proc
    if _static_proc is not None:
        try:
            _static_proc.terminate()
        except OSError:
            pass
        _static_proc = None
    try:
        os.remove(_LOADING_PID_FILE)
    except OSError:
        pass


# ---- THE DASHBOARD SEAM (ai-visualizer/dashboard). Same rule as above:
# every write is wrapped, the bus must never crash the voice line.
#   .voice_events      JSONL: tool calls, tool results, subagent/task life
#   .voice_permission  JSON while a spoken permission ask is pending
#   .voice_context     JSON: context-window percent after each turn
#   .voice_caps        JSON: what this backtalk offers the dashboard
_EVENTS_FILE = os.path.join(_DIR, ".voice_events")
_PERMISSION_FILE = os.path.join(_DIR, ".voice_permission")
_CONTEXT_FILE = os.path.join(_DIR, ".voice_context")
_CAPS_FILE = os.path.join(_DIR, ".voice_caps")
_EVENTS_MAX_BYTES = 512 * 1024


# Masked BEFORE anything reaches disk: a Bash command can carry a key.
_SECRET_RES = [
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+|basic\s+)?\S+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=\-]{8,}"),
    re.compile(r"(?i)((?:api[_-]?key|apikey|access[_-]?key|secret|token|passw(?:or)?d|pwd|client[_-]?secret)"
               r"[\"']?\s*[:=]\s*[\"']?)[^\s\"'&]+"),
    re.compile(r"(?i)(--?(?:password|passwd|token|api-key|secret)[ =])\S+"),
    re.compile(r"(?i)(security\s+add-generic-password\b.*?\s-w\s+)\S+"),
    re.compile(r"(?i)((?:\s-u|--user)\s+[^:\s]+:)\S+"),          # curl -u user:pass
    re.compile(r"(://[^/\s:@]+:)[^@\s/]+(?=@)"),                    # scheme://user:pass@
    re.compile(r"()\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_\-]{16,}"
               r"|AKIA[0-9A-Z]{16}|xox[abprs]-[A-Za-z0-9\-]{10,}|AIza[0-9A-Za-z_\-]{30,}"
               r"|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,})"),
    re.compile(r"()(?<![/\w.-])[A-Za-z0-9+_\-]{32,}={0,2}(?![/\w.])"),   # long random strings
]


def mask_secrets(value):
    """Mask anything key-, token- or password-shaped, recursively."""
    if isinstance(value, str):
        for rx in _SECRET_RES:
            value = rx.sub(lambda m: m.group(1) + "[masked]", value)
        return value
    if isinstance(value, dict):
        return {k: mask_secrets(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_secrets(v) for v in value]
    return value


def event(kind: str, **fields):
    """Append one event line (secrets masked); rotate past 512 KB."""
    try:
        fields = mask_secrets(fields)
        try:
            if os.path.getsize(_EVENTS_FILE) > _EVENTS_MAX_BYTES:
                os.replace(_EVENTS_FILE, _EVENTS_FILE + ".1")
        except OSError:
            pass
        with open(_EVENTS_FILE, "a") as f:
            f.write(json.dumps({"ts": time.time(), "kind": kind, **fields},
                               default=str) + "\n")
    except Exception:
        pass


def permission_pending(tool: str, what: str, detail: str):
    try:
        with open(_PERMISSION_FILE, "w") as f:
            f.write(json.dumps({"ts": time.time(), "tool": tool,
                                "what": what, "detail": detail}))
    except Exception:
        pass


def permission_clear():
    try:
        os.remove(_PERMISSION_FILE)
    except OSError:
        pass


def set_context(percentage, total=None, maximum=None):
    try:
        with open(_CONTEXT_FILE, "w") as f:
            f.write(json.dumps({"ts": time.time(), "percentage": percentage,
                                "total": total, "max": maximum}))
    except Exception:
        pass


def set_caps(**caps):
    """Empty caps = this backtalk is gone; the dashboard greys its buttons."""
    try:
        with open(_CAPS_FILE, "w") as f:
            f.write(json.dumps(caps))
    except Exception:
        pass
