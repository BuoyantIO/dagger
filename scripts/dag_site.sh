#!/usr/bin/env bash
# Serve the live DAG viewer and open it in the browser; reuse a page this root already runs.
#
# Usage:
#   dag/scripts/dag_site.sh
#   PORT=8767 dag/scripts/dag_site.sh
#
# Env vars and defaults:
#   PORT=8766    port for the live viewer
set -euo pipefail

PORT=${PORT:-8766}

# Run from the project root so dag paths resolve from any cwd.
cd "$(dirname "$0")/../.."
root=$PWD
url="http://127.0.0.1:$PORT/"

# Only open the browser when a page for this root already runs, e.g. one dag_orch.sh started.
if pgrep -f "$root/dag/src/tm.py serve" >/dev/null; then
  echo "[-] page already running, opening $url"
  open "$url"
  exit 0
fi

# Open the browser once the server answers, polling for up to 5 s.
(
  for _ in $(seq 1 50); do
    if curl -fs -o /dev/null "$url"; then
      open "$url"
      break
    fi
    sleep 0.1
  done
) &

# Serve in the foreground so Ctrl-C stops it.
echo "[-] serving $url"
python3 "$root/dag/src/tm.py" serve --port "$PORT"
