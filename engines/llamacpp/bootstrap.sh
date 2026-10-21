#!/usr/bin/env bash
# ablit bootstrap for the llama.cpp server. It runs the nine stages in order, and
# each finished stage writes a marker under /ablit/stages/, so a restart skips it.
#
# Steps that need a real rented machine are marked
#   untested on a rented machine
set -euo pipefail
set -E

ENGINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The uploaded layout keeps common.sh beside the engine scripts.
if [ -f "$ENGINE_DIR/common.sh" ]; then ABLIT_LIB="${ABLIT_LIB:-$ENGINE_DIR/common.sh}"; fi
# shellcheck source=../lib/common.sh
. "${ABLIT_LIB:-$ENGINE_DIR/../lib/common.sh}"
# shellcheck source=/dev/null
[ -f "${ABLIT_STATE_DIR}/env" ] && { set -a; . "${ABLIT_STATE_DIR}/env"; set +a; }

export ABLIT_READY_TIMEOUT_S="${ABLIT_READY_TIMEOUT_S:-600}"

stage_start()   { ablit_write_run_file; ablit_start_watchdog; }
stage_check()   { ablit_check_hardware; }
stage_randread(){ ablit_random_read_test; }
stage_speed()   { ablit_speed_check; }
stage_download(){ ablit_download_all; }
stage_staging() { ablit_log "a single GGUF file needs no staging"; }

# Stage 7: start llama-server on 127.0.0.1. The key travels in LLAMA_API_KEY.
# untested on a rented machine.
stage_engine() {
  ablit_require_env ABLIT_PORT ABLIT_ENDPOINT_KEY ABLIT_SERVED_NAME ABLIT_CONTEXT_TOKENS
  local model bin
  model="$(ablit_paths_by_role weight | head -n 1)"
  [ -f "$model" ] || ablit_fail "missing_file" "no weight file found under $ABLIT_DATA_DIR"
  bin="${LLAMA_SERVER_BIN:-$(command -v llama-server || echo /app/llama-server)}"
  [ -x "$bin" ] || ablit_fail "missing_tool" "llama-server was not found at $bin"
  ablit_start_engine LLAMA_API_KEY \
    "$bin" -m "$model" --host 127.0.0.1 --port "$ABLIT_PORT" \
    --ctx-size "$ABLIT_CONTEXT_TOKENS" --parallel 1 --n-gpu-layers 99 --alias "$ABLIT_SERVED_NAME"
}

stage_health()  { ablit_health_check; }
stage_ready()   { ablit_write_ready; }

main() {
  trap 'ablit_fail command_failed "A bootstrap command failed."' ERR
  # A process can be gone after a container restart even when its stage finished.
  if [ ! -f "$ABLIT_STATE_DIR/engine.pid" ] || ! kill -0 "$(cat "$ABLIT_STATE_DIR/engine.pid")" 2>/dev/null; then
    rm -f "$ABLIT_STATE_DIR/stages/07-engine.done" "$ABLIT_STATE_DIR/stages/08-health.done" \
      "$ABLIT_STATE_DIR/stages/09-ready.done" "$ABLIT_STATE_DIR/ready.json"
  fi
  # After a restart the run file exists but the watchdog process may be gone.
  if [ -f "$ABLIT_STATE_DIR/run.json" ]; then ablit_start_watchdog; fi
  ablit_run_stage 01 start    stage_start
  ablit_run_stage 02 check    stage_check
  ablit_run_stage 03 randread stage_randread
  ablit_run_stage 04 speed    stage_speed
  ablit_run_stage 05 download stage_download
  ablit_run_stage 06 staging  stage_staging
  ablit_run_stage 07 engine   stage_engine
  ablit_run_stage 08 health   stage_health
  ablit_run_stage 09 ready    stage_ready
}

main "$@"
