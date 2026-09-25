#!/bin/bash
# Call-mode Jarvis: a SECOND backtalk copy for phone calls. Listens to
# BlackHole 16ch (the caller), speaks into BlackHole 2ch (the call's mic).
# Deliberately does NOT go through run.sh: run.sh's single-instance guard
# pkills any other backtalk.main, which would kill the normal desk Jarvis.
# A pidfile replaces only a previous call-mode copy.
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export BACKTALK_CONFIG="$PWD/backtalk-call.json"
PIDFILE="$PWD/../call-mode/call.pid"
if [ -f "$PIDFILE" ]; then
  old=$(cat "$PIDFILE")
  if kill -0 "$old" 2>/dev/null; then kill "$old"; sleep 1; fi
fi
echo $$ > "$PIDFILE"
# Not exec: when the call copy quits (or is killed), hang up the Google Voice call too.
uv run python -m backtalk.main --open-mic "$@" &
CHILD=$!
trap 'kill $CHILD 2>/dev/null' TERM INT
wait $CHILD
node "$PWD/../sms-watch/end-call.mjs"
