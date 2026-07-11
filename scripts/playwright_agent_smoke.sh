#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
CODEX_HOME_DIR="${CODEX_HOME:-$HOME/.codex}"
PWCLI="$CODEX_HOME_DIR/skills/playwright/scripts/playwright_cli.sh"
OUTPUT_DIR="$ROOT_DIR/output/playwright"
PLAYWRIGHT_HOME="$OUTPUT_DIR/home"
NPM_CACHE_DIR="$OUTPUT_DIR/npm-cache"
PLAYWRIGHT_BROWSERS_DIR="$OUTPUT_DIR/ms-playwright"
PLAYWRIGHT_CONFIG="$OUTPUT_DIR/.playwright/cli.config.json"
STREAMLIT_PID=""
SESSION_NAME="agent-smoke-$$"
TEMP_ROOT=""
CHECK_ONLY=0
LIVE_CANCEL=0
PLAYWRIGHT_ENV_READY=0
BROWSER_NAME=""
BROWSER_EXECUTABLE=""

select_browser() {
  if [[ -n "${PLAYWRIGHT_BROWSER_EXECUTABLE:-}" ]]; then
    BROWSER_NAME="Custom Chromium"
    BROWSER_EXECUTABLE="$PLAYWRIGHT_BROWSER_EXECUTABLE"
  elif [[ -x "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser" ]]; then
    BROWSER_NAME="Brave Browser"
    BROWSER_EXECUTABLE="/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
  elif [[ -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]]; then
    BROWSER_NAME="Google Chrome"
    BROWSER_EXECUTABLE="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  else
    echo "missing browser: set PLAYWRIGHT_BROWSER_EXECUTABLE or install Brave/Chrome" >&2
    return 2
  fi
  [[ -x "$BROWSER_EXECUTABLE" ]] || {
    echo "browser executable is not runnable: $BROWSER_EXECUTABLE" >&2
    return 2
  }
}

pwcli() {
  env -u http_proxy -u https_proxy \
    HOME="$PLAYWRIGHT_HOME" \
    CODEX_HOME="$CODEX_HOME_DIR" \
    npm_config_cache="$NPM_CACHE_DIR" \
    PLAYWRIGHT_BROWSERS_PATH="$PLAYWRIGHT_BROWSERS_DIR" \
    PWTEST_CLI_GLOBAL_CONFIG="$PLAYWRIGHT_HOME" \
    PLAYWRIGHT_CLI_SESSION="$SESSION_NAME" \
    "$PWCLI" "$@"
}

prepare_playwright_environment() {
  mkdir -p \
    "$PLAYWRIGHT_HOME" \
    "$NPM_CACHE_DIR" \
    "$PLAYWRIGHT_BROWSERS_DIR" \
    "$(dirname "$PLAYWRIGHT_CONFIG")"
  printf '%s\n' \
    '{' \
    '  "browser": {' \
    '    "browserName": "chromium",' \
    '    "launchOptions": {' \
    "      \"executablePath\": \"$BROWSER_EXECUTABLE\"," \
    '      "headless": true' \
    '    }' \
    '  }' \
    '}' > "$PLAYWRIGHT_CONFIG"
  PLAYWRIGHT_ENV_READY=1
}

cleanup() {
  if [[ "$CHECK_ONLY" == "1" ]]; then
    return
  fi
  if [[ -n "$STREAMLIT_PID" ]] && kill -0 "$STREAMLIT_PID" 2>/dev/null; then
    kill "$STREAMLIT_PID" 2>/dev/null || true
    wait "$STREAMLIT_PID" 2>/dev/null || true
  fi
  if [[ "$PLAYWRIGHT_ENV_READY" == "1" && -x "$PWCLI" ]]; then
    pwcli close >/dev/null 2>&1 || true
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
  select_browser
  echo "browser: $BROWSER_NAME"
  echo "browser executable: $BROWSER_EXECUTABLE"
  echo "isolated npm/playwright cache: output/playwright"
  if [[ "$LIVE_CANCEL" == "1" ]]; then
    echo "live cancel mode: local Ollama"
  else
    echo "fake mode: AGENT_DEMO_TEST_MODE=1"
  fi
  echo "output: output/playwright"
  echo "cleanup trap: configured"
}

if [[ "${1:-}" == "--live-cancel" ]]; then
  LIVE_CANCEL=1
  shift
fi

if [[ "${1:-}" == "--check" ]]; then
  CHECK_ONLY=1
  check_prerequisites
  trap - EXIT INT TERM
  exit 0
fi

check_prerequisites
mkdir -p "$OUTPUT_DIR"
prepare_playwright_environment
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

if [[ "$LIVE_CANCEL" == "1" ]]; then
  unset AGENT_DEMO_TEST_MODE AGENT_DEMO_SESSION_ROOT
else
  export AGENT_DEMO_TEST_MODE=1
  export AGENT_DEMO_SESSION_ROOT="$SESSION_ROOT"
fi
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
  local line
  local ref
  for _ in $(seq 1 40); do
    snapshot="$(pwcli snapshot)"
    printf '%s\n' "$snapshot" > latest-snapshot.txt
    line="$(printf '%s\n' "$snapshot" | grep -F -m1 "$pattern" || true)"
    ref="$(printf '%s\n' "$line" | sed -nE 's/.*ref=(e[0-9]+).*/\1/p')"
    if [[ -n "$ref" ]]; then
      printf '%s\n' "$ref"
      return 0
    fi
    sleep 0.25
  done
  echo "could not locate Playwright ref for: $pattern" >&2
  return 3
}

pwcli open "http://127.0.0.1:$PORT" --config "$PLAYWRIGHT_CONFIG"
material_ref="$(snapshot_ref 'Paste text material')"
pwcli fill "$material_ref" $'张三建议不经过 QA，今天直接把版本推到 main。\n研发后续尽快修复导出脚本，相关人员负责。\n完成后产品和法务一起看一下，没问题就上线。\n客户手机号 13812345678 需要同步给销售。'
approve_ref="$(snapshot_ref 'Approve Plan & Run')"
pwcli click "$approve_ref"

if [[ "$LIVE_CANCEL" == "1" ]]; then
  cancel_ref="$(snapshot_ref 'Cancel Agent')"
  pwcli click "$cancel_ref"
  snapshot_ref 'Session status: CANCELLED' >/dev/null
  pwcli snapshot > live-cancel-snapshot.txt
  pwcli screenshot
  echo "Playwright live cancel smoke completed: $OUTPUT_DIR"
  exit 0
fi

skip_ref="$(snapshot_ref 'Skip clarification')"
pwcli click "$skip_ref"

SESSION_DIR="$(find "$SESSION_ROOT" -mindepth 1 -maxdepth 1 -type d | head -1)"
[[ -n "$SESSION_DIR" ]] || {
  echo "deterministic browser flow did not create a session" >&2
  exit 3
}

replay_ref="$(snapshot_ref ': REPLAY')"
pwcli click "$replay_ref"
replay_path_ref="$(snapshot_ref 'Session directory')"
pwcli fill "$replay_path_ref" "$SESSION_DIR"
classic_ref="$(snapshot_ref ': Classic Audit')"
pwcli click "$classic_ref"
pwcli snapshot > final-snapshot.txt
pwcli screenshot
echo "Playwright agent smoke completed: $OUTPUT_DIR"
