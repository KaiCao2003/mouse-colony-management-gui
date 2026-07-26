#!/bin/zsh

set -euo pipefail
umask 077

script_dir="${0:A:h}"
cd "$script_dir"

if ! command -v uv >/dev/null 2>&1; then
  print -u2 "Mouse Colony Management GUI requires uv, but 'uv' was not found."
  exit 127
fi

print "Preparing Mouse Colony Management GUI..."
uv sync --locked

local_port="$(uv run python -c 'from app.config import get_settings; print(get_settings().local_port)')"
local_log_level="$(uv run python -c 'from app.config import get_settings; print(get_settings().log_level.lower())')"

print ""
print "Mouse Colony Management GUI"
print "Open http://127.0.0.1:${local_port}"
print "The server is available on this computer only. Press Control-C to stop."
print ""

exec uv run uvicorn app.main:create_app --factory \
  --host 127.0.0.1 \
  --port "$local_port" \
  --log-level "$local_log_level"
