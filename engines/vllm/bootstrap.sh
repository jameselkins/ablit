#!/usr/bin/env bash
# ablit bootstrap for the vLLM engine. It runs the nine stages in order, and each
# finished stage writes a marker under /ablit/stages/, so a restart skips it.
#
# Steps that need a real rented machine are marked
#   TODO(step 3): untested
set -euo pipefail

ENGINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
. "${ABLIT_LIB:-$ENGINE_DIR/../lib/common.sh}"
[ -f "${ABLIT_STATE_DIR}/env" ] && { set -a; . "${ABLIT_STATE_DIR}/env"; set +a; }

# vLLM pins host memory for the Engram tables of the DeepSeek build, and the
# locked-memory limit must allow it. TODO(step 3): untested. The figure is the
# 183 GiB in the vLLM recipe, in kilobytes.
case "${ABLIT_MODEL_ID:-}" in
  ds41-1017-abl-fp8) export ABLIT_MIN_MEMLOCK_KB="${ABLIT_MIN_MEMLOCK_KB:-191889408}" ;;
esac
export ABLIT_READY_TIMEOUT_S="${ABLIT_READY_TIMEOUT_S:-3700}"

stage_start()   { ablit_write_run_file; ablit_start_watchdog; }
stage_check()   { ablit_check_hardware; }
stage_randread(){ ablit_random_read_test; }
stage_speed()   { ablit_speed_check; }
stage_download(){ ablit_download_all; }
stage_staging() { ablit_log "vLLM reads the downloaded files as they are, so there is nothing to stage"; }

# Stage 7: build the command line and start vLLM on 127.0.0.1.
# The key travels in VLLM_API_KEY, not on the command line.
# TODO(step 3): untested. Confirm that this vLLM release reads VLLM_API_KEY.
stage_engine() {
  ablit_require_env ABLIT_PORT ABLIT_ENDPOINT_KEY ABLIT_SERVED_NAME ABLIT_GPU_COUNT ABLIT_CONTEXT_TOKENS
  local -a args=(
    vllm serve "$ABLIT_DATA_DIR"
    --served-model-name "$ABLIT_SERVED_NAME"
    --host 127.0.0.1
    --port "$ABLIT_PORT"
    --tensor-parallel-size "$ABLIT_GPU_COUNT"
    --max-model-len "$ABLIT_CONTEXT_TOKENS"
  )
  case "$ABLIT_MODEL_ID" in
    ds41-1017-abl-fp8)
      # Flags from the vLLM recipe for DeepSeek V4.1 Flash on 8 GPUs with 80 GB each.
      args+=(
        --tokenizer-mode deepseek_v41
        --engram-config '{"cpu_offload":true}'
        --max-num-batched-tokens 4096
        --gpu-memory-utilization 0.92
        --reasoning-parser deepseek_v41
        --tool-call-parser deepseek_v41
        --enable-auto-tool-choice
      )
      export VLLM_USE_V2_MODEL_RUNNER=1
      export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
      export VLLM_ENGINE_READY_TIMEOUT_S=3600
      ;;
    *)
      : # Other builds need only the common flags above.
      ;;
  esac
  ablit_start_engine VLLM_API_KEY "${args[@]}"
}

stage_health()  { ablit_health_check; }
stage_ready()   { ablit_write_ready; }

main() {
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
