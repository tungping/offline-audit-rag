#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PWCLI="${CODEX_HOME:-$HOME/.codex}/skills/playwright/scripts/playwright_cli.sh"
OUTPUT_DIR="$ROOT_DIR/output/playwright"
STREAMLIT_PID=""
SESSION_NAME="agent-smoke-$$"
TEMP_ROOT=""

cleanup() {
  if [[ -n "$STREAMLIT_PID" ]] && kill -0 "$STREAMLIT_PID" 2>/dev/null; then
    kill "$STREAMLIT_PID" 2>/dev/null || true
    wait "$STREAMLIT_PID" 2>/dev/null || true
  fi
  if [[ -x "$PWCLI" ]]; then
    PLAYWRIGHT_CLI_SESSION="$SESSION_NAME" "$PWCLI" close >/dev/null 2>&1 || true
  fi
  if [[ -n "$TEMP_ROOT" && -d "$TEMP_ROOT" ]]; then
    find "$TEMP_ROOT" -type f -delete 2>/dev/null || true
    find "$TEMP_ROOT" -depth -type d -empty -delete 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

check_prerequisites() {
  command -v npx >/dev/null 2>&1 || {
    echo "missing prerequisite: npx" >&2
    return 2
  }
  echo "npx: ok"
  [[ -x "$PWCLI" ]] || {
    echo "missing Playwright wrapper: $PWCLI" >&2
    return 2
  }
  echo "playwright wrapper: ok"
  [[ -x "$ROOT_DIR/.venv/bin/streamlit" ]] || {
    echo "missing Streamlit executable: $ROOT_DIR/.venv/bin/streamlit" >&2
    return 2
  }
  echo "streamlit: ok"
  echo "fake mode: AGENT_DEMO_TEST_MODE=1"
  echo "output: output/playwright"
  echo "cleanup trap: configured"
}

if [[ "${1:-}" == "--check" ]]; then
  check_prerequisites
  exit 0
fi

check_prerequisites
mkdir -p "$OUTPUT_DIR"
TEMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/agent-playwright.XXXXXX")"
SESSION_ROOT="$TEMP_ROOT/sessions"
mkdir -p "$SESSION_ROOT"
PORT="$($ROOT_DIR/.venv/bin/python - <<'PY'
import socket
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)"

export AGENT_DEMO_TEST_MODE=1
export AGENT_DEMO_SESSION_ROOT="$SESSION_ROOT"
export PLAYWRIGHT_CLI_SESSION="$SESSION_NAME"

cd "$ROOT_DIR"
"$ROOT_DIR/.venv/bin/streamlit" run webui.py \
  --server.headless true \
  --server.address 127.0.0.1 \
  --server.port "$PORT" \
  >"$OUTPUT_DIR/streamlit.log" 2>&1 &
STREAMLIT_PID=$!

for _ in $(seq 1 60); do
  if "$ROOT_DIR/.venv/bin/python" - "$PORT" <<'PY' >/dev/null 2>&1
import sys
import urllib.request
urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/_stcore/health", timeout=0.5).read()
PY
  then
    break
  fi
  sleep 0.25
done

"$ROOT_DIR/.venv/bin/python" - "$PORT" <<'PY'
import sys
import urllib.request
urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/_stcore/health", timeout=1).read()
PY

cd "$OUTPUT_DIR"

snapshot_ref() {
  local pattern="$1"
  local snapshot
  snapshot="$("$PWCLI" snapshot)"
  printf '%s\n' "$snapshot" > latest-snapshot.txt
  local line
  line="$(printf '%s\n' "$snapshot" | grep -F -m1 "$pattern" || true)"
  local ref
  ref="$(printf '%s\n' "$line" | sed -nE 's/.*ref=(e[0-9]+).*/\1/p')"
  if [[ -z "$ref" ]]; then
    echo "could not locate Playwright ref for: $pattern" >&2
    return 3
  fi
  printf '%s\n' "$ref"
}

"$PWCLI" open "http://127.0.0.1:$PORT"
material_ref="$(snapshot_ref 'Paste text material')"
"$PWCLI" fill "$material_ref" $'张三建议不经过 QA，今天直接把版本推到 main。\n研发后续尽快修复导出脚本，相关人员负责。\n完成后产品和法务一起看一下，没问题就上线。\n客户手机号 13812345678 需要同步给销售。'
approve_ref="$(snapshot_ref 'Approve Plan & Run')"
"$PWCLI" click "$approve_ref"
"$PWCLI" run-code "await page.waitForTimeout(1500)"
skip_ref="$(snapshot_ref 'Skip clarification')"
"$PWCLI" click "$skip_ref"
"$PWCLI" run-code "await page.waitForTimeout(1500)"

SESSION_DIR="$(find "$SESSION_ROOT" -mindepth 1 -maxdepth 1 -type d | head -1)"
[[ -n "$SESSION_DIR" ]] || {
  echo "deterministic browser flow did not create a session" >&2
  exit 3
}

replay_ref="$(snapshot_ref 'REPLAY')"
"$PWCLI" click "$replay_ref"
replay_path_ref="$(snapshot_ref 'Session directory')"
"$PWCLI" fill "$replay_path_ref" "$SESSION_DIR"
"$PWCLI" run-code "await page.waitForTimeout(500)"
classic_ref="$(snapshot_ref 'Classic Audit')"
"$PWCLI" click "$classic_ref"
"$PWCLI" run-code "await page.waitForTimeout(500)"
"$PWCLI" snapshot > final-snapshot.txt
"$PWCLI" screenshot
echo "Playwright agent smoke completed: $OUTPUT_DIR"
