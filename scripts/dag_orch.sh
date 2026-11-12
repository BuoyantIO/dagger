#!/usr/bin/env bash
# Start the unblock daemon and status page if absent, then hand the terminal to the orchestrator.
#
# Usage:
#   dag/scripts/dag_orch.sh
#   dag/scripts/dag_orch.sh --checklist <path>
#   dag/scripts/dag_orch.sh --spec <path>
#   dag/scripts/dag_orch.sh --task "<text>"
#   PORT=8767 dag/scripts/dag_orch.sh
#
# Env vars and defaults:
#   PORT=8766    status page port
set -euo pipefail

PORT=${PORT:-8766}

# Build the optional intake line; paths go absolute before the cd changes what they resolve against.
intake=
case "${1:-}" in
  "") ;;
  --checklist | --spec)
    path=${2:?usage: dag/scripts/dag_orch.sh $1 <path>}
    intake="Intake: ${1#--} $(cd "$(dirname "$path")" && pwd)/$(basename "$path")" ;;
  --task)
    intake="Intake: task ${2:?usage: dag/scripts/dag_orch.sh --task \"<text>\"}" ;;
  *)
    echo "[-] unknown argument $1, expected --checklist, --spec or --task" >&2
    exit 1 ;;
esac

# Run from the project root so dag paths resolve from any cwd.
cd "$(dirname "$0")/../.."
root=$PWD

# The data folder of a fresh project may lack the runtime dir that holds logs and task records.
mkdir -p dag_data/runs

# Start unblock unless one runs for this root; the absolute path keeps other projects' daemons out of the match.
if pgrep -f "$root/dag/src/unblock.py" >/dev/null; then
  echo "[-] unblock already running"
else
  nohup python3 "$root/dag/src/unblock.py" >>dag_data/runs/unblock.out 2>&1 &
  echo "[-] unblock started"
fi

# Start the status page the same way.
if pgrep -f "$root/dag/src/tm.py serve" >/dev/null; then
  echo "[-] page already running"
else
  nohup python3 "$root/dag/src/tm.py" serve --port "$PORT" >>dag_data/runs/serve.out 2>&1 &
  echo "[-] page started"
fi
echo "[-] page: http://localhost:$PORT"

# Append the intake line after a blank line, when one was given.
prompt=$(cat dag/ORCHESTRATOR_PROMPT.md)
if [ -n "$intake" ]; then
  prompt="$prompt"$'\n\n'"$intake"
fi

# Hand the terminal to Claude with the takeover prompt as its first message.
exec claude "$prompt"
