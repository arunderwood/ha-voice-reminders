#!/usr/bin/env bash
# Run every suite and summarise.
#
#   . .venv/bin/activate
#   ./tools/check_all.sh --offline      # grammar only; no Home Assistant needed
#
#   export HA_URL=http://homeassistant.local:8123
#   export HA_TOKEN=...                 # Profile -> Long-lived access tokens
#   ./tools/check_all.sh                # everything (~10 min)
#   ./tools/check_all.sh --fast         # skips the two that wait on the clock
#
# The offline suites read the grammar in THIS REPO, not whatever is deployed, so
# run them before copying files to Home Assistant. The live suites talk to HA
# over HTTP and websocket, so they run from anywhere that can reach it.
#
# Anything that touches the reminder list snapshots it first and puts it back.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-python3}"
MODE=${1:-all}

case "$MODE" in
  --offline|--fast|all|"") ;;
  *) echo "usage: $0 [--offline|--fast]" >&2; exit 2 ;;
esac

if [[ "$MODE" != "--offline" ]]; then
  : "${HA_TOKEN:?set HA_TOKEN, or pass --offline to run grammar checks only}"
fi

declare -a NAMES RESULTS
overall=0

run_suite () {   # label, script
  local name=$1 script=$2
  printf '\n\033[1m==> %s\033[0m\n' "$name"
  local out
  out=$("$PY" "$ROOT/tools/$script" 2>&1)
  # Every suite ends with a FAILURES line; show that and any headline count
  # rather than whatever happened to print last.
  echo "$out" | grep -E '^swept |^FAILURES: ' | sed 's/^/    /'
  local fails
  fails=$(echo "$out" | sed -n 's/^FAILURES: \([0-9]*\)$/\1/p' | tail -1)
  [[ -z "$fails" ]] && { fails="?"; echo "$out" | tail -n 5 | sed 's/^/    /'; }
  NAMES+=("$name"); RESULTS+=("$fails")
  [[ "$fails" != "0" ]] && overall=1
  return 0
}

run_suite "grammar (offline, ~1s)"   reminders_check.py
run_suite "phrasing sweep (offline)" reminders_sweep.py

if [[ "$MODE" != "--offline" ]]; then
  run_suite "end to end"                 reminders_e2e.py
  run_suite "list + cancel"              reminders_manage_test.py
  if [[ "$MODE" != "--fast" ]]; then
    run_suite "recurring series"         reminders_recurring_test.py
    run_suite "dispatch (announce+push)" reminders_dispatch_test.py
  else
    echo -e "\n==> skipping recurring + dispatch (--fast)"
  fi
fi

printf '\n\033[1m===== summary =====\033[0m\n'
for i in "${!NAMES[@]}"; do
  if [[ "${RESULTS[$i]}" == "0" ]]; then
    printf '  \033[32mPASS\033[0m  %s\n' "${NAMES[$i]}"
  else
    printf '  \033[31mFAIL\033[0m  %s  (%s)\n' "${NAMES[$i]}" "${RESULTS[$i]}"
  fi
done
[[ $overall -eq 0 ]] && echo -e "\nall green" || echo -e "\nsomething failed"
exit $overall
