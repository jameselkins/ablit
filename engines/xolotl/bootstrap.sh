#!/usr/bin/env bash
# ablit bootstrap for the Xolotl engine. It runs the nine stages in order, and
# each finished stage writes a marker under /ablit/stages/, so a restart skips it.
#
# Steps that need a real rented machine are marked
#   TODO(step 3): untested
# Steps that need the Xolotl engine itself are marked
#   TODO(step 5): untested
set -euo pipefail

ENGINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
. "${ABLIT_LIB:-$ENGINE_DIR/../lib/common.sh}"
# shellcheck source=/dev/null
[ -f "${ABLIT_STATE_DIR}/env" ] && { set -a; . "${ABLIT_STATE_DIR}/env"; set +a; }

XOLOTL_DIR="${XOLOTL_DIR:-/opt/xolotl}"
XOLOTL_COMMIT="63fc34c6ea839d3e78e765b3e3aa72173116d352"
JOINED_DIR="$ABLIT_DATA_DIR/joined"
ENGRAM_DIR="$ABLIT_DATA_DIR/engram"

# The engine pins about 60 GiB of host memory by default, and the locked-memory
# limit must allow it. The figure is in kilobytes.
export ABLIT_MIN_MEMLOCK_KB="${ABLIT_MIN_MEMLOCK_KB:-62914560}"
export ABLIT_RANDOM_READ_REQUIRED="${ABLIT_RANDOM_READ_REQUIRED:-1}"
export ABLIT_READY_TIMEOUT_S="${ABLIT_READY_TIMEOUT_S:-900}"

stage_start()   { ablit_write_run_file; ablit_start_watchdog; }
stage_check()   { ablit_check_hardware; }
stage_randread(){ ablit_random_read_test; }
stage_speed()   { ablit_speed_check; }
stage_download(){ ablit_download_all; }

# Stage 6, part one: join the weight parts into one file, in name order.
# The parts are deleted only after the joined file has the expected size.
# TODO(step 5): untested. The part order and the join output name come from the
# catalog entry's file names and have not been run against the engine.
join_weights() {
  local marker
  marker="$(ablit_stage_marker 06 join)"
  [ -f "$marker" ] && return 0
  local -a parts
  mapfile -t parts < <(ablit_paths_by_role weight)
  [ "${#parts[@]}" -gt 0 ] || ablit_fail "missing_file" "the entry lists no weight files"
  mkdir -p "$JOINED_DIR"
  local out="$JOINED_DIR/model.gguf" expected=0 part
  for part in "${parts[@]}"; do
    expected=$((expected + $(ablit_file_size "$part")))
  done
  if [ "${#parts[@]}" -eq 1 ]; then
    mv "${parts[0]}" "$out"
  else
    cat "${parts[@]}" >"$out.tmp"
    [ "$(ablit_file_size "$out.tmp")" -eq "$expected" ] || ablit_fail "join_size_mismatch" "$out.tmp"
    mv "$out.tmp" "$out"
    if [ "${ABLIT_DELETE_PARTS:-1}" = "1" ]; then
      rm -f "${parts[@]}"
    fi
  fi
  date +%s >"$marker"
}

# Stage 6, part two: extract the Engram tables from the two source shards.
# Both shards stay on disk until extraction finishes. Entries that already hold
# extracted tables (role "engram") need no extraction.
# TODO(step 5): untested. The extractor arguments follow the engine README at the
# pinned commit and have not been run. The extractor may also need config.json and
# model.safetensors.index.json from the base repo, and the entry does not list them.
extract_engram() {
  local marker
  marker="$(ablit_stage_marker 06 extract)"
  [ -f "$marker" ] && return 0
  local -a sources
  mapfile -t sources < <(ablit_paths_by_role engram-source)
  if [ "${#sources[@]}" -gt 0 ]; then
    mkdir -p "$ENGRAM_DIR/layers.1" "$ENGRAM_DIR/layers.14"
    python3 "$XOLOTL_DIR/tools/v41/extract_engram.py" "$(dirname "${sources[0]}")" \
      --to "layers.1=$ENGRAM_DIR/layers.1" --to "layers.14=$ENGRAM_DIR/layers.14" \
      || ablit_fail "engram_extract_failed" "extract_engram.py exited with an error"
    if [ "${ABLIT_DELETE_ENGRAM_SOURCE:-1}" = "1" ]; then
      rm -f "${sources[@]}"
    fi
  fi
  date +%s >"$marker"
}

stage_staging() {
  join_weights
  extract_engram
}

# Use the prebuilt engine if the image has it. Otherwise build it on the box.
# TODO(step 5): untested. The CMake flags are not known yet and come from the
# engine README at the pinned commit through XOLOTL_CMAKE_FLAGS.
ensure_engine() {
  [ -f "$XOLOTL_DIR/tools/v41/serve/serve_ds41.py" ] && return 0
  command -v git >/dev/null 2>&1 || ablit_fail "missing_tool" "git is needed to build the engine"
  git clone --quiet https://github.com/Indras-Mirror/Xolotl "$XOLOTL_DIR"
  git -C "$XOLOTL_DIR" checkout --quiet "$XOLOTL_COMMIT"
  ( cd "$XOLOTL_DIR"
    bash tools/xolotl/fetch-llama.sh
    # shellcheck disable=SC2086
    cmake -S . -B build-xolotl -G Ninja ${XOLOTL_CMAKE_FLAGS:-}
    ninja -C build-xolotl ) || ablit_fail "engine_build_failed" "building Xolotl at $XOLOTL_COMMIT failed"
}

# Stage 7: render the config and start the engine on 127.0.0.1.
# The key travels in STRATA_API_KEY, not on the command line.
# TODO(step 5): untested. The config keys come from the config file that ships
# with the engine at the pinned commit. Copy that file, then set the model, rank,
# expert map, draft model, tokenizer and Engram paths found by role under /models.
stage_engine() {
  ablit_require_env ABLIT_PORT ABLIT_ENDPOINT_KEY ABLIT_SERVED_NAME
  ensure_engine
  local config="$ABLIT_STATE_DIR/xolotl-config.json"
  cp "$XOLOTL_DIR/tools/v41/serve/strata-ds41.json" "$config"
  ablit_log "config copied from the engine. Paths must be patched before the first real run."
  ablit_start_engine STRATA_API_KEY \
    python3 "$XOLOTL_DIR/tools/v41/serve/serve_ds41.py" \
    --engine strata --config "$config" --host 127.0.0.1 --port "$ABLIT_PORT"
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
