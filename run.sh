#!/usr/bin/env bash
# Start OCI Monitor. Creates .venv on first run.
#
#   ./run.sh [flags]              run in the foreground (Ctrl+C to stop)
#   ./run.sh start [flags]        run in the background, logging to ocimon.log
#   ./run.sh stop                 stop the background instance
#   ./run.sh restart [flags]      stop, then start in the background
#   ./run.sh status               is it running, and where
#   ./run.sh logs                 follow the background log (Ctrl+C stops following, not the app)
#
# Flags go to `python -m ocimon`, e.g. ./run.sh start --regions all
set -euo pipefail
cd "$(dirname "$0")"

PID_FILE=.ocimon.pid
LOG_FILE=ocimon.log

ensure_venv() {
  local python="${PYTHON:-python3}"
  if [ ! -x .venv/bin/python ]; then
    if ! "$python" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
      echo "OCI Monitor needs Python 3.10 or newer ($python is $("$python" --version 2>&1))." >&2
      exit 1
    fi
    echo "Creating virtual environment in .venv ..."
    if ! "$python" -m venv .venv; then
      rm -rf .venv  # a half-created venv would make every later run fail
      echo "Could not create a virtual environment. On Ubuntu/Debian run: sudo apt install python3-venv" >&2
      exit 1
    fi
  fi
  # (Re)install when the venv is new or requirements.txt changed (e.g. after git pull).
  if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ]; then
    echo "Installing dependencies ..."
    .venv/bin/python -m pip install --quiet --upgrade pip
    .venv/bin/python -m pip install --quiet -r requirements.txt
    touch .venv/.installed
  fi
}

running() { [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

url_from_log() { grep -o 'on http[^ ]*' "$LOG_FILE" 2>/dev/null | tail -1 | cut -c4-; }

start() {
  ensure_venv
  if running; then
    echo "OCI Monitor is already running (pid $(cat "$PID_FILE")) at $(url_from_log)"
    return 0
  fi
  local offset=0
  [ -f "$LOG_FILE" ] && offset=$(wc -c < "$LOG_FILE")
  nohup .venv/bin/python -m ocimon "$@" >> "$LOG_FILE" 2>&1 < /dev/null &
  local pid=$!
  echo "$pid" > "$PID_FILE"
  for _ in $(seq 1 100); do  # up to ~20s
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$PID_FILE"
      echo "OCI Monitor failed to start:" >&2
      tail -c +"$((offset + 1))" "$LOG_FILE" | tail -5 >&2
      return 1
    fi
    if tail -c +"$((offset + 1))" "$LOG_FILE" | grep -q "Uvicorn running"; then
      echo "OCI Monitor is running in the background (pid $pid) at $(url_from_log)"
      echo "Logs: ./run.sh logs    Stop: ./run.sh stop"
      return 0
    fi
    sleep 0.2
  done
  echo "Started (pid $pid) but it isn't serving yet; check ./run.sh logs"
}

stop() {
  if ! running; then
    rm -f "$PID_FILE"
    echo "OCI Monitor is not running"
    return 0
  fi
  local pid; pid=$(cat "$PID_FILE")
  kill "$pid"
  for _ in $(seq 1 50); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.2
  done
  kill -0 "$pid" 2>/dev/null && kill -9 "$pid"
  rm -f "$PID_FILE"
  echo "OCI Monitor stopped"
}

case "${1:-}" in
  start)   shift; start "$@" ;;
  stop)    stop ;;
  restart) shift; stop; start "$@" ;;
  status)
    if running; then echo "OCI Monitor is running (pid $(cat "$PID_FILE")) at $(url_from_log)"
    else echo "OCI Monitor is not running"; exit 3; fi ;;
  logs)    touch "$LOG_FILE"; exec tail -n 50 -f "$LOG_FILE" ;;
  *)       ensure_venv; exec .venv/bin/python -m ocimon "$@" ;;
esac
