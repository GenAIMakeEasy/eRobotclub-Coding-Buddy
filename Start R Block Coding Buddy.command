#!/bin/bash
# Double-click to start R Block Coding Buddy.
# Starts Ollama if it isn't already running, then the app server, then a
# Cloudflare Tunnel so it's reachable off the school wifi too, and opens
# the app in your browser once it's ready.

cd "$(dirname "$0")" || exit 1

PORT=8420
MODEL="gemma4:12b-it-qat"

echo "R Block Coding Buddy"
echo "===================="

# --- Ollama ---------------------------------------------------------------
if ! curl -s -m 2 http://localhost:11434/api/version >/dev/null 2>&1; then
  echo "Starting Ollama..."
  open -a Ollama 2>/dev/null || ollama serve >/tmp/rblock-ollama.log 2>&1 &
  for i in $(seq 1 30); do
    curl -s -m 1 http://localhost:11434/api/version >/dev/null 2>&1 && break
    sleep 1
  done
fi

if ! curl -s -m 2 http://localhost:11434/api/version >/dev/null 2>&1; then
  echo "Could not reach Ollama. Please start it manually and try again."
  read -n 1 -s -r -p "Press any key to close..."
  exit 1
fi

if ! ollama list 2>/dev/null | grep -q "$MODEL"; then
  echo "Model $MODEL is not installed. Downloading it now (this can take a while)..."
  ollama pull "$MODEL"
fi

# --- App server -------------------------------------------------------------
# Free the port if a previous instance is still holding it
if lsof -ti:"$PORT" >/dev/null 2>&1; then
  echo "Port $PORT is in use — stopping previous instance..."
  lsof -ti:"$PORT" | xargs kill -9 2>/dev/null
  sleep 1
fi
echo "Starting the app server on port $PORT..."
export OLLAMA_MODEL="$MODEL"
python3 -m uvicorn app.api.main:app --host 0.0.0.0 --port "$PORT" &
SERVER_PID=$!

# --- Cloudflare Tunnel ------------------------------------------------------
# A Quick Tunnel (no Cloudflare account or domain needed) -- the public URL
# it prints is randomly generated fresh every time this script runs, so
# it's not the same link twice. Fine for reaching the app from off the
# school wifi right now; a stable, unchanging address needs a real domain
# routed through Cloudflare (a Named Tunnel) instead.
#
# Caught live: when this runs at login (via the LaunchAgent, RunAtLoad),
# it can fire before wifi has even associated yet, not just before DNS
# has settled -- cloudflared then fails its very first connection attempt
# and just exits (no retry of its own), silently leaving no public link
# for the rest of the session. A fixed "sleep 10" here would be a guess
# (too short some days, wasted time on others) -- waiting for a REAL
# network check to succeed, capped at 30s, adapts to however long it
# actually takes instead of guessing. The retry loop below is kept as a
# backstop for a hiccup that happens after this check already passed.
echo "Waiting for network..."
for i in $(seq 1 30); do
  curl -s -m 2 -o /dev/null https://1.1.1.1 && break
  sleep 1
done

TUNNEL_PID=""
TUNNEL_LOG="/tmp/rblock-cloudflared.log"
if command -v cloudflared >/dev/null 2>&1; then
  for attempt in 1 2 3; do
    : > "$TUNNEL_LOG"
    cloudflared tunnel --url "http://localhost:$PORT" > "$TUNNEL_LOG" 2>&1 &
    TUNNEL_PID=$!
    for i in $(seq 1 15); do
      grep -q 'trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null && break
      kill -0 "$TUNNEL_PID" 2>/dev/null || break
      sleep 1
    done
    if grep -q 'trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null; then
      break
    fi
    echo "Cloudflare Tunnel attempt $attempt didn't come up -- retrying..."
    kill "$TUNNEL_PID" 2>/dev/null
    TUNNEL_PID=""
    sleep 2
  done
fi

# Both processes die together when this window closes -- the whole point
# of running the server this way instead of as a silent background
# service (see scripts note in the LaunchAgent plist / session history).
trap "echo Stopping...; kill $SERVER_PID 2>/dev/null; [ -n \"$TUNNEL_PID\" ] && kill $TUNNEL_PID 2>/dev/null" EXIT

for i in $(seq 1 30); do
  curl -s -m 1 "http://localhost:$PORT/health" >/dev/null 2>&1 && break
  sleep 1
done

LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)
echo ""
echo "Ready."
echo "  On this Mac:      http://localhost:$PORT"
[ -n "$LAN_IP" ] && echo "  On the same wifi:  http://$LAN_IP:$PORT"

if [ -n "$TUNNEL_PID" ]; then
  TUNNEL_URL=""
  for i in $(seq 1 20); do
    TUNNEL_URL=$(grep -o 'https://[a-zA-Z0-9.-]*trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | head -1)
    [ -n "$TUNNEL_URL" ] && break
    sleep 1
  done
  if [ -n "$TUNNEL_URL" ]; then
    echo "  From anywhere:     $TUNNEL_URL  (changes every restart, share it fresh each time)"
    echo ""
    if command -v qrencode >/dev/null 2>&1; then
      echo "  Scan to open on a phone:"
      qrencode -t ANSIUTF8 -m 2 "$TUNNEL_URL"
    fi
  else
    echo "  Cloudflare Tunnel didn't come up in time -- check $TUNNEL_LOG"
  fi
fi

echo ""
echo "Keep this window open while students are using it."
echo "Close this window (or press Ctrl+C) to stop everything."

open "http://localhost:$PORT"

wait $SERVER_PID
