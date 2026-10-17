#!/usr/bin/env bash
# One-command test runner with a LIVE report.
#
#   bash scripts/run_all.sh              # direct-mode suite (incl. adversarial) + Studionet end-to-end
#   bash scripts/run_all.sh unit         # fast suite only (no network needed after install)
#   bash scripts/run_all.sh adversarial  # only the hostile-input tests
#   bash scripts/run_all.sh e2e          # only the live Studionet end-to-end test
#
# While it runs, open the report (it refreshes itself every 3 seconds):
#   Codespaces: Ports tab -> "Live test report" (port 8000) -> open in browser
#   elsewhere:  http://localhost:8000
# Files are also left in reports/<suite>/report.{html,md,json}.
#
# Useful variables: REPORT_PORT (8000)  NO_SERVE=1  SKIP_INSTALL=1  KEEP_SERVING=0
#   E2E_CONTRACT_ADDRESS=0x...  (attach the e2e test to a deployed, fresh contract)
#   E2E_SETTLE=skip|wait|resume   E2E_TIME=real|warp   E2E_EVIDENCE=live|mock   GLTEST_ARGS="--leader-only"
set -uo pipefail
cd "$(dirname "$0")/.."

WHAT="${1:-all}"
PORT="${REPORT_PORT:-8000}"
PYTEST_CMD="${PYTEST_CMD:-python3 -m pytest}"
GLTEST_CMD="${GLTEST_CMD:-gltest}"
case "$WHAT" in all|unit|adversarial|e2e) ;; *) echo "usage: $0 [all|unit|adversarial|e2e]"; exit 2 ;; esac

mkdir -p reports
if [ ! -f reports/index.html ]; then
  echo '<meta http-equiv="refresh" content="2"><p>Starting…</p>' > reports/index.html
fi

if [ "${SKIP_INSTALL:-0}" != "1" ]; then
  echo "==> installing dependencies"
  python3 -m pip install -q -r requirements.txt || { echo "dependency install failed"; exit 2; }
fi

SERVER_PID=""
if [ "${NO_SERVE:-0}" != "1" ]; then
  python3 -m http.server "$PORT" --directory reports >/dev/null 2>&1 &
  SERVER_PID=$!
  if [ -n "${CODESPACE_NAME:-}" ]; then
    URL="https://${CODESPACE_NAME}-${PORT}.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"
  else
    URL="http://localhost:${PORT}"
  fi
  echo "==> live report: ${URL}   (refreshes itself every 3 s)"
fi

status=0
run() {  # run <dir> <title> <command...>
  local dir="$1" title="$2"; shift 2
  echo; echo "==> ${title}"
  LIVE_REPORT_DIR="reports/${dir}" LIVE_REPORT_TITLE="${title}" "$@" || status=1
}

if [ "$WHAT" = "all" ] || [ "$WHAT" = "unit" ]; then
  run unit "Direct-mode suite (all tests, including adversarial)" $PYTEST_CMD tests -v
fi
if [ "$WHAT" = "adversarial" ]; then
  run adversarial "Adversarial suite" $PYTEST_CMD tests -m adversarial -v
fi
if [ "$WHAT" = "all" ] || [ "$WHAT" = "e2e" ]; then
  if command -v "${GLTEST_CMD%% *}" >/dev/null 2>&1; then
    E2E_NETWORK="${E2E_NETWORK:-studionet}" run studionet "Studionet end-to-end" \
      $GLTEST_CMD --network "${E2E_NETWORK:-studionet}" tests_e2e -v ${GLTEST_ARGS:-}
  else
    echo "!! '${GLTEST_CMD}' not found: is genlayer-test installed? (pip install -r requirements.txt)"; status=1
  fi
fi

echo
echo "==> reports written to reports/ (index.html, <suite>/report.html|md|json)"
[ "$status" -eq 0 ] && echo "==> ALL GREEN" || echo "==> FAILURES: open the report for details"

if [ -n "$SERVER_PID" ]; then
  if [ -t 1 ] && [ "${KEEP_SERVING:-1}" = "1" ]; then
    echo "==> report server still running on port ${PORT}; press Ctrl-C to stop it"
    trap 'kill $SERVER_PID 2>/dev/null; exit $status' INT TERM
    wait "$SERVER_PID"
  fi
  kill "$SERVER_PID" 2>/dev/null
fi
exit $status
