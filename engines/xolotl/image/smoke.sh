#!/usr/bin/env bash
# Smoke test for the ablit Xolotl image. It needs no GPU.
#
# usage: smoke.sh python | binary | all
#
#   python  checks the tools and Python packages, the server's help text, the
#           Engram script, and a mock server that must accept a right key and
#           refuse a missing or wrong key. It needs no compiled binary.
#   binary  checks that ds41_generate has every library it needs and that it
#           starts far enough to print its usage text. This needs the stub of
#           the GPU driver library, which only the smoke stage of the image has.
#   all     runs both.
#
# The test cannot load a model or touch a GPU, so it shows that the files are
# in place and the programs start. It does not show that the engine computes
# the right answer.
set -euo pipefail

XOLOTL_DIR="${XOLOTL_DIR:-/opt/xolotl}"
ENGINE="$XOLOTL_DIR/build-xolotl/ds41_generate"
SERVER="$XOLOTL_DIR/tools/v41/serve/serve_ds41.py"
MOCK_PORT="${SMOKE_PORT:-18150}"
mock_pid=""

fail() {
  echo "SMOKE FAILED: $*" >&2
  exit 1
}

cleanup() {
  if [ -n "$mock_pid" ]; then
    kill "$mock_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT

check_files() {
  local path
  for path in "$@"; do
    [ -e "$path" ] || fail "missing file $path"
  done
}

check_python() {
  echo "== tools"
  local tool
  for tool in bash curl python3 awk sha256sum stat dd; do
    command -v "$tool" >/dev/null 2>&1 || fail "$tool is not installed"
  done
  python3 --version
  curl --version | head -n 1

  echo "== files"
  check_files "$SERVER" \
    "$XOLOTL_DIR/tools/v41/serve/strata-ds41.json" \
    "$XOLOTL_DIR/tools/v41/serve/dspark_expert_counts.txt" \
    "$XOLOTL_DIR/tools/v41/extract_engram.py" \
    "$XOLOTL_DIR/tools/v41/dsv41_encoding.py" \
    "$XOLOTL_DIR/tools/strata_tokenizer.py" \
    "$XOLOTL_DIR/serve/server.py" \
    "$XOLOTL_DIR/serve/web/index.html" \
    "$XOLOTL_DIR/bin/ds41-engine" \
    "$XOLOTL_DIR/BUILD-INFO" \
    "$XOLOTL_DIR/licenses/NOTICE" \
    "$XOLOTL_DIR/licenses/xolotl-LICENSE" \
    "$XOLOTL_DIR/licenses/llama.cpp-LICENSE"
  cat "$XOLOTL_DIR/BUILD-INFO"

  echo "== python packages"
  python3 -I -c 'import jinja2, markupsafe, regex, psutil; print("jinja2", jinja2.__version__, "regex", regex.__version__, "psutil", psutil.__version__)'

  echo "== server help text"
  local help
  help="$(python3 -I "$SERVER" --engine strata --help 2>&1)" || fail "serve_ds41.py --help exited with an error: $help"
  printf '%s\n' "$help" | grep -q -- '--config' || fail "serve_ds41.py --help does not mention --config"

  echo "== Engram script"
  local out code=0
  out="$(python3 -I "$XOLOTL_DIR/tools/v41/extract_engram.py" 2>&1)" || code=$?
  [ "$code" -eq 2 ] || fail "extract_engram.py with no arguments should exit 2, it exited $code"
  printf '%s\n' "$out" | grep -q 'extract_engram.py' || fail "extract_engram.py printed no usage text"

  echo "== mock server and API key"
  # This starts the generic Xolotl server with its mock engine. The DeepSeek
  # wrapper in serve_ds41.py needs the real tokenizer files, so it cannot start
  # with the mock engine, and the help text check above covers its imports.
  # The key goes in the environment, as the real run does. Output goes to a
  # file, and the key never appears in it.
  local key log
  key="smoke-$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')"
  log="$(mktemp)"
  (cd "$XOLOTL_DIR" && exec env STRATA_API_KEY="$key" python3 -m serve.server --engine mock \
    --host 127.0.0.1 --port "$MOCK_PORT") >"$log" 2>&1 &
  mock_pid=$!
  local waited=0 status
  while :; do
    status="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 5 "http://127.0.0.1:$MOCK_PORT/health" || true)"
    [ "$status" = "200" ] && break
    kill -0 "$mock_pid" 2>/dev/null || { cat "$log" >&2; fail "the mock server stopped before it answered"; }
    waited=$((waited + 1))
    [ "$waited" -lt 60 ] || { cat "$log" >&2; fail "the mock server did not answer /health in 60 seconds"; }
    sleep 1
  done
  status="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 10 \
    -H "Authorization: Bearer $key" "http://127.0.0.1:$MOCK_PORT/v1/models")"
  [ "$status" = "200" ] || fail "/v1/models with the right key returned $status"
  status="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 10 "http://127.0.0.1:$MOCK_PORT/v1/models")"
  [ "$status" = "401" ] || fail "/v1/models with no key returned $status, expected 401"
  status="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 10 \
    -H "Authorization: Bearer wrong-$key" "http://127.0.0.1:$MOCK_PORT/v1/models")"
  [ "$status" = "401" ] || fail "/v1/models with a wrong key returned $status, expected 401"
  kill "$mock_pid" 2>/dev/null || true
  wait "$mock_pid" 2>/dev/null || true
  mock_pid=""
  rm -f "$log"
  echo "python checks passed"
}

check_binary() {
  echo "== engine binary"
  check_files "$ENGINE"
  local missing
  missing="$(ldd "$ENGINE" | grep 'not found' || true)"
  [ -z "$missing" ] || fail "the engine has libraries that are not installed: $missing"
  local out code=0
  out="$("$ENGINE" --help 2>&1)" || code=$?
  [ "$code" -eq 2 ] || fail "ds41_generate --help should exit 2 after its usage text, it exited $code: $out"
  printf '%s\n' "$out" | grep -q 'usage' || fail "ds41_generate printed no usage text: $out"
  if [ -f "$XOLOTL_DIR/ELF-ARCHS" ]; then
    echo "GPU code in the binary: $(tr '\n' ' ' <"$XOLOTL_DIR/ELF-ARCHS")"
  fi
  echo "binary checks passed"
}

case "${1:-all}" in
  python) check_python ;;
  binary) check_binary ;;
  all) check_python; check_binary ;;
  *) echo "usage: smoke.sh python | binary | all" >&2; exit 2 ;;
esac
