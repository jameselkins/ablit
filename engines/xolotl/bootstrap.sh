#!/usr/bin/env bash
# ablit bootstrap for the Xolotl engine. It runs the nine stages in order, and
# each finished stage writes a marker under /ablit/stages/, so a restart skips it.
#
# The engine, its web server and the Engram extraction script are already in the
# container image (engines/xolotl/image). This script only downloads the model
# files, prepares them and starts the server on 127.0.0.1.
#
# Every minute on the rented machine costs money, so the work overlaps:
#   1. A join worker starts first. It appends each weight part to the joined file
#      as soon as the part is verified, and deletes the part, while the next
#      parts are still downloading.
#   2. Downloads run in a fixed priority order. Files that need work afterward
#      come before files that need none, so the last download has the least work
#      behind it. The default order is the small files, then the weight parts and
#      the draft model, then the two Engram source files, which need no work.
#   3. With ABLIT_ENGRAM_MODE=extract the two Engram source files come first, and
#      an extraction worker copies the Engram tables out of them while the weight
#      parts download.
#
# Engram modes:
#   direct   (default) The engine reads the two official safetensors files in
#            place. Its source (Ds41Engram::open at the pinned commit) parses the
#            safetensors headers itself, so the 203 GB copy that
#            extract_engram.py makes is not needed. If the engine cannot open the
#            files, this script runs the extraction and starts the engine again.
#   extract  Run extract_engram.py while the weights download.
#
# Steps that need a real rented machine are marked
#   TODO(step 3): untested on a rented machine
# Steps that need the Xolotl engine itself are marked
#   TODO(step 5): untested on a rented machine
set -euo pipefail
set -E

ENGINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The uploaded layout keeps common.sh beside the engine scripts.
if [ -f "$ENGINE_DIR/common.sh" ]; then ABLIT_LIB="${ABLIT_LIB:-$ENGINE_DIR/common.sh}"; fi
# shellcheck source=../lib/common.sh
. "${ABLIT_LIB:-$ENGINE_DIR/../lib/common.sh}"
# shellcheck source=/dev/null
[ -f "${ABLIT_STATE_DIR}/env" ] && { set -a; . "${ABLIT_STATE_DIR}/env"; set +a; }

# --- Paths and settings, all in one place ------------------------------------

# Paths inside the container image. See engines/xolotl/image/README.md.
XOLOTL_DIR="${XOLOTL_DIR:-/opt/xolotl}"
XOLOTL_BINARY="${XOLOTL_BINARY:-$XOLOTL_DIR/build-xolotl/ds41_generate}"
XOLOTL_ENGINE_EXE="${XOLOTL_ENGINE_EXE:-$XOLOTL_DIR/bin/ds41-engine}"
XOLOTL_SERVER="${XOLOTL_SERVER:-$XOLOTL_DIR/tools/v41/serve/serve_ds41.py}"
XOLOTL_EXTRACTOR="${XOLOTL_EXTRACTOR:-$XOLOTL_DIR/tools/v41/extract_engram.py}"
# The image packages are installed for the system Python. A login shell on a
# rented machine may put another Python first on the path, so name it.
if [ -x /usr/bin/python3 ]; then
  XOLOTL_PYTHON="${XOLOTL_PYTHON:-/usr/bin/python3}"
else
  XOLOTL_PYTHON="${XOLOTL_PYTHON:-python3}"
fi

# Paths on the rented machine. Relative names are under the data directory.
JOINED_REL="joined/model.gguf"       # the joined weight file
ENGRAM_OUT_REL="engram"              # where extract_engram.py writes, one folder per layer
ENGRAM_LAYERS="1 14"                 # the Engram layers of DeepSeek-V4.1-Flash
XOLOTL_CONFIG="$ABLIT_STATE_DIR/xolotl-config.json"
XOLOTL_CORE_LOG="$ABLIT_STATE_DIR/logs/engine-core.log"

# Settings. The arena size matches the locked-memory limit in the engine lock.
ENGRAM_MODE="${ABLIT_ENGRAM_MODE:-direct}"
XOLOTL_ARENA_GIB="${XOLOTL_ARENA_GIB:-60}"
XOLOTL_USE_DRAFT="${XOLOTL_USE_DRAFT:-1}"
# The engine at the pinned commit keeps finished conversations in pinned host
# memory (--conversation-cache-mib). Its own default is 8192 MiB, taken a piece at
# a time as conversations are saved (ds41_generate.cpp, "int conv_mib = 8192").
# The 60 GiB arena already uses most of a 90 GB host, so this script asks for half
# of that. A value of 0 turns the pool off, which is how the earlier pin ran.
XOLOTL_CONVERSATION_CACHE_MIB="${XOLOTL_CONVERSATION_CACHE_MIB:-4096}"

# Download settings. Chunks are small so that the checksum of a file can follow
# the download closely, and so that the last file is spread over many connections.
export ABLIT_DOWNLOAD_CHUNK_BYTES="${ABLIT_DOWNLOAD_CHUNK_BYTES:-268435456}"
export ABLIT_DOWNLOAD_SPLIT_BYTES="${ABLIT_DOWNLOAD_SPLIT_BYTES:-536870912}"
HEAVY_FILES="${ABLIT_DOWNLOAD_FILES:-2}"
HEAVY_RANGES="${ABLIT_DOWNLOAD_RANGES:-16}"
SMALL_ROLES=(engram-index tokenizer rank config expert-map checksum script)
if [ "$ENGRAM_MODE" = "extract" ]; then
  HEAVY_ROUNDS=("engram-source engram" "weight draft")
else
  HEAVY_ROUNDS=("weight draft" "engram-source engram")
fi

# The speed check is a short part of the real download, with the same number of
# connections. Its measure ignores the first seconds, when connections speed up.
export ABLIT_SPEED_TEST_SECONDS="${ABLIT_SPEED_TEST_SECONDS:-45}"
# The engine pins about 60 GiB of host memory by default, and the locked-memory
# limit must allow it. The figure is in kilobytes.
export ABLIT_MIN_MEMLOCK_KB="${ABLIT_MIN_MEMLOCK_KB:-62914560}"
export ABLIT_RANDOM_READ_REQUIRED="${ABLIT_RANDOM_READ_REQUIRED:-1}"
# Loading 163 GB of weights takes the engine one to two minutes. The wait ends
# early when the server process dies.
export ABLIT_READY_TIMEOUT_S="${ABLIT_READY_TIMEOUT_S:-900}"
export ABLIT_HEALTH_POLL_S="${ABLIT_HEALTH_POLL_S:-2}"

WORKERS_STARTED=0

# --- stages 1 to 4 -----------------------------------------------------------

stage_start()   { ablit_write_run_file; ablit_start_watchdog; }

stage_check() {
  case "$ENGRAM_MODE" in
    direct|extract) ;;
    *) ablit_fail "bad_variable" "ABLIT_ENGRAM_MODE must be direct or extract" ;;
  esac
  # Check the image first. A wrong image should fail now, not after the download.
  [ -x "$XOLOTL_BINARY" ] || ablit_fail "missing_file" "the engine binary $XOLOTL_BINARY is not in the image"
  [ -x "$XOLOTL_ENGINE_EXE" ] || ablit_fail "missing_file" "the engine wrapper $XOLOTL_ENGINE_EXE is not in the image"
  [ -f "$XOLOTL_SERVER" ] || ablit_fail "missing_file" "the server $XOLOTL_SERVER is not in the image"
  [ -f "$XOLOTL_EXTRACTOR" ] || ablit_fail "missing_file" "the Engram script $XOLOTL_EXTRACTOR is not in the image"
  ablit_check_hardware
  if [ "$ENGRAM_MODE" = "extract" ]; then
    # Extraction writes a second copy of the Engram tables next to the sources.
    local need free
    need="$(awk -v g="$ABLIT_MIN_DISK_GB" -v e="$(ablit_bytes_by_role engram-source)" 'BEGIN {printf "%.0f", g * 1e9 + e}')"
    free="$(ablit_free_bytes "$ABLIT_DATA_DIR")"
    ablit_ge "$free" "$need" || ablit_fail "disk" "$free bytes are free, extract mode needs $need"
  fi
}

stage_randread(){ ablit_random_read_test; }

# TODO(step 3): untested on a rented machine.
stage_speed() {
  # shellcheck disable=SC2086
  ABLIT_DOWNLOAD_FILES="$HEAVY_FILES" ABLIT_DOWNLOAD_RANGES="$HEAVY_RANGES" \
    ablit_speed_check ${HEAVY_ROUNDS[0]}
}

# --- stage 5: download, with the join (and the extraction) running beside it -

# usage: download_round FILES RANGES [ROLE...]
# Downloads the files with these roles, in the order of the roles. With no roles
# it downloads everything that is still missing.
download_round() {
  local files="$1" ranges="$2"
  shift 2
  ABLIT_DOWNLOAD_FILES="$files" ABLIT_DOWNLOAD_RANGES="$ranges" ablit_download_all "$@"
}

# The whole download, run as one background step so the main script can watch the
# other steps while it runs.
download_job() {
  local round
  download_round 8 1 "${SMALL_ROLES[@]}"
  for round in "${HEAVY_ROUNDS[@]}"; do
    # shellcheck disable=SC2086
    download_round "$HEAVY_FILES" "$HEAVY_RANGES" $round
  done
  download_round "$HEAVY_FILES" "$HEAVY_RANGES"
}

# Append the weight parts to one file in name order, as each part is verified.
# The last part is the only one left to append when the download ends.
# TODO(step 5): untested on a rented machine. The part order is the name order,
# which the catalog entries and the upstream reassemble.sh scripts both use.
join_worker() {
  ablit_step_done join && return 0
  local -a rels
  mapfile -t rels < <(ablit_rels_by_role weight)
  [ "${#rels[@]}" -gt 0 ] || ablit_fail "missing_file" "the entry lists no weight files"
  mkdir -p "$ABLIT_DATA_DIR/$(dirname "$JOINED_REL")"
  if [ "${#rels[@]}" -eq 1 ]; then
    # One file needs no join. Rename it, and remember that it was used up.
    if [ -e "$ABLIT_DATA_DIR/${rels[0]}" ] || [ ! -f "$ABLIT_DATA_DIR/$JOINED_REL" ]; then
      ablit_wait_for_files "$ABLIT_DATA_DIR/${rels[0]}"
      mv "$ABLIT_DATA_DIR/${rels[0]}" "$ABLIT_DATA_DIR/$JOINED_REL"
    fi
    ablit_mark_consumed "${rels[0]}"
  else
    ablit_join_parts "$JOINED_REL" "$(ablit_bytes_by_role weight)" "${ABLIT_DELETE_PARTS:-1}" "${rels[@]}"
  fi
  ablit_step_mark join
}

# Print the folders that hold the Engram tables, joined with colons.
engram_argument() {
  local -a rels dirs=()
  local rel dir known seen layer
  mapfile -t rels < <(ablit_rels_by_role engram)
  if [ "${#rels[@]}" -gt 0 ]; then
    # The entry lists tables that are already extracted.
    for rel in "${rels[@]}"; do
      dir="$(dirname "$ABLIT_DATA_DIR/$rel")"
      seen=0
      for known in ${dirs[@]+"${dirs[@]}"}; do [ "$known" != "$dir" ] || seen=1; done
      [ "$seen" = 1 ] || dirs+=("$dir")
    done
  elif ablit_step_done extract; then
    for layer in $ENGRAM_LAYERS; do dirs+=("$ABLIT_DATA_DIR/$ENGRAM_OUT_REL/layers.$layer"); done
  else
    # Read the official files in place: the folder holds the index and both shards.
    mapfile -t rels < <(ablit_rels_by_role engram-source)
    [ "${#rels[@]}" -gt 0 ] || ablit_fail "missing_file" "the entry lists no Engram files"
    dirs=("$(dirname "$ABLIT_DATA_DIR/${rels[0]}")")
    [ -f "${dirs[0]}/model.safetensors.index.json" ] \
      || ablit_fail "missing_file" "model.safetensors.index.json is not next to the Engram source files"
  fi
  (IFS=:; printf '%s' "${dirs[*]}")
}

# Copy the Engram tables out of the two source files into one file per layer.
# Fails the run when it cannot, because the caller has no other way to go on.
# TODO(step 5): untested on a rented machine. The arguments follow the script's
# own usage text at the pinned commit.
run_extraction() {
  local -a rels to=()
  local rel src total free layer pid watcher status=0
  mapfile -t rels < <(ablit_rels_by_role engram-source)
  [ "${#rels[@]}" -gt 0 ] || ablit_fail "missing_file" "the entry lists no Engram source files"
  for rel in "${rels[@]}"; do
    [ -f "$ABLIT_DATA_DIR/$rel" ] || ablit_fail "missing_file" "$rel is not on the box"
  done
  src="$(dirname "$ABLIT_DATA_DIR/${rels[0]}")"
  total="$(ablit_bytes_by_role engram-source)"
  free="$(ablit_free_bytes "$ABLIT_DATA_DIR")"
  ablit_ge "$free" "$((total + 2147483648))" \
    || ablit_fail "disk" "extraction needs about $total free bytes and $ABLIT_DATA_DIR has $free"
  for layer in $ENGRAM_LAYERS; do
    mkdir -p "$ABLIT_DATA_DIR/$ENGRAM_OUT_REL/layers.$layer"
    to+=(--to "layers.$layer=$ABLIT_DATA_DIR/$ENGRAM_OUT_REL/layers.$layer")
  done
  mkdir -p "$ABLIT_STATE_DIR/logs"
  ablit_log "extracting the Engram tables"
  "$XOLOTL_PYTHON" "$XOLOTL_EXTRACTOR" "$src" "${to[@]}" >>"$ABLIT_STATE_DIR/logs/extract.log" 2>&1 &
  pid=$!
  # Report how far the script got, from the size of what it has written.
  "$XOLOTL_PYTHON" "$ABLIT_FETCH" watch --state "$ABLIT_STATE_DIR" --label extract --total "$total" \
    --pid "$pid" --interval 5 "$ABLIT_DATA_DIR/$ENGRAM_OUT_REL" >/dev/null 2>&1 &
  watcher=$!
  wait "$pid" || status=$?
  wait "$watcher" || true
  [ "$status" -eq 0 ] || ablit_fail "engram_extract_failed" "extract_engram.py exited with code $status, see logs/extract.log"
  if [ "${ABLIT_DELETE_ENGRAM_SOURCE:-0}" = "1" ]; then
    for rel in "${rels[@]}"; do ablit_consume "$rel"; done
  fi
  ablit_step_mark extract
}

extract_worker() {
  ablit_step_done extract && return 0
  local -a rels paths=()
  local rel
  mapfile -t rels < <(ablit_rels_by_role engram-source)
  [ "${#rels[@]}" -gt 0 ] || return 0
  for rel in "${rels[@]}"; do paths+=("$ABLIT_DATA_DIR/$rel"); done
  ablit_wait_for_files "${paths[@]}"
  run_extraction
}

# Start the workers once. A restart that skips stage 5 starts them in stage 6.
start_workers() {
  [ "$WORKERS_STARTED" = 0 ] || return 0
  WORKERS_STARTED=1
  ablit_spawn join join_worker
  if [ "$ENGRAM_MODE" = "extract" ]; then ablit_spawn extract extract_worker; fi
}

# TODO(step 3): untested on a rented machine.
stage_download() {
  start_workers
  ablit_spawn download download_job
  # Returns when the download ends. A failed worker ends the script at once.
  ablit_wait_jobs download
}

# What is left after the download is the last join, and in extract mode the last
# of the extraction. In the best case both have finished already.
stage_staging() {
  start_workers
  ablit_wait_jobs
}

# --- stage 7: write the server config and start the engine -------------------

export_engine_environment() {
  # A login over ssh may not carry the image environment, so set what the engine needs.
  export XOLOTL_DIR STRATA_TEXT_LOG=0 PYTHONUNBUFFERED=1
  export LD_LIBRARY_PATH="/usr/local/nvidia/lib:/usr/local/nvidia/lib64:/usr/local/cuda/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
}

# Print the first path with a role, in name order. It reads all the input, so no
# earlier command in the pipe ends on a closed pipe.
first_rel() { ablit_rels_by_role "$1" | sed -n 1p; }

# Write the server config as JSON. The values mirror the sample config that the
# Xolotl authors ship, with the paths of this machine. The API key is not in it:
# the server reads the key from its environment.
# TODO(step 5): untested on a rented machine. The keys are those of the sample
# configs at the pinned commit and have not been run.
write_server_config() {
  local engram="$1" model rank tokenizer draft=""
  model="$ABLIT_DATA_DIR/$JOINED_REL"
  rank="$ABLIT_DATA_DIR/$(first_rel rank)"
  tokenizer="$(dirname "$ABLIT_DATA_DIR/$(first_rel tokenizer)")"
  if [ "$XOLOTL_USE_DRAFT" = "1" ]; then
    draft="$(first_rel draft)"
    [ -z "$draft" ] || draft="$ABLIT_DATA_DIR/$draft"
  fi
  [ -f "$rank" ] || ablit_fail "missing_file" "no rank file found under $ABLIT_DATA_DIR"
  [ -d "$tokenizer" ] || ablit_fail "missing_file" "no tokenizer folder found under $ABLIT_DATA_DIR"
  mkdir -p "$ABLIT_STATE_DIR/logs"
  "$XOLOTL_PYTHON" -I - "$XOLOTL_CONFIG" "$XOLOTL_ENGINE_EXE" "$XOLOTL_DIR" "$model" "$rank" "$tokenizer" \
    "$engram" "$draft" "$ABLIT_CONTEXT_TOKENS" "$XOLOTL_ARENA_GIB" "$ABLIT_SERVED_NAME" "$ABLIT_PORT" \
    "$XOLOTL_CORE_LOG" "$XOLOTL_CONVERSATION_CACHE_MIB" <<'PY'
import json
import os
import sys

(out, exe, cwd, model, rank, tokenizer, engram, draft, ctx, arena, name, port, log, pool) = sys.argv[1:15]
env = {"STRATA_ATTN_F16_GATHER": "1"}
if draft:
    env["STRATA_SERVE_DSPARK"] = draft
config = {
    "exe": exe,
    "args": [
        "-m", model, "--backend", "cuda", "--experts", "gpu", "--engram", engram, "--rank", rank,
        "--ctx", ctx, "--stop", "1", "--arena-adapt", "--kv-fp4", "--route-bias", "0.5",
        "--route-bias-file", "0.3", "--arena-gib", arena, "--prompt-batch", "12288",
        "--conversation-cache-mib", str(int(pool)),
    ],
    "cwd": cwd,
    "tokenizer": tokenizer,
    "model_name": name,
    "log": log,
    "host": "127.0.0.1",
    "port": int(port),
    "gpu": 0,
    "sampling": {"temperature": 1, "top_p": 0.95},
    "api_monitor": False,
    "reasoning_budget_tokens": 32768,
    "env": env,
}
temporary = out + ".tmp"
with open(temporary, "w", encoding="utf-8") as handle:
    json.dump(config, handle, indent=1)
os.replace(temporary, out)
PY
}

# Start the server. The key travels in STRATA_API_KEY, not on the command line.
launch_server() {
  write_server_config "$1"
  ablit_launch_engine STRATA_API_KEY "$XOLOTL_PYTHON" "$XOLOTL_SERVER" \
    --engine strata --config "$XOLOTL_CONFIG" --host 127.0.0.1 --port "$ABLIT_PORT"
}

# The server opens its port only after the engine reports that the model is
# loaded. Returns 0 when the port is open and 1 when the server process ended.
wait_for_listener() {
  local waited=0 poll=2
  while :; do
    if ablit_port_open "$ABLIT_PORT"; then return 0; fi
    ablit_pid_alive "$ABLIT_ENGINE_PID" || return 1
    waited=$((waited + poll))
    [ "$waited" -lt "$ABLIT_READY_TIMEOUT_S" ] \
      || ablit_fail "health_timeout" "the engine did not open its port within ${ABLIT_READY_TIMEOUT_S}s"
    sleep "$poll"
  done
}

# Succeeds when the engine logs written after a byte offset say that it could not
# read the Engram files. These are the messages of Ds41Engram::open.
engram_open_failed_since() {
  local mark="$1" text="" log
  for log in "$ABLIT_STATE_DIR/logs/engine.log" "$XOLOTL_CORE_LOG"; do
    [ -f "$log" ] || continue
    text="$text$(tail -c +"$((mark + 1))" "$log" 2>/dev/null || true)"
  done
  grep -Eq 'engram: .*(no model\.safetensors\.index\.json|not in the index|not found in the given dirs|cannot read|unexpected dtype|cannot open)' <<<"$text"
}

log_size() {
  if [ -f "$1" ]; then wc -c <"$1" | tr -d ' '; else echo 0; fi
}

# TODO(step 5): untested on a rented machine. This is the first time the engine
# runs here, and reading the Engram files in place is a guess from the source.
stage_engine() {
  ablit_require_env ABLIT_PORT ABLIT_ENDPOINT_KEY ABLIT_SERVED_NAME ABLIT_CONTEXT_TOKENS
  [ -f "$ABLIT_DATA_DIR/$JOINED_REL" ] || ablit_fail "missing_file" "the joined weight file is not in $ABLIT_DATA_DIR"
  export_engine_environment
  local mark_engine mark_core engram tail_text
  mark_engine="$(log_size "$ABLIT_STATE_DIR/logs/engine.log")"
  mark_core="$(log_size "$XOLOTL_CORE_LOG")"
  engram="$(engram_argument)"
  ablit_log "starting the engine, Engram files from $engram"
  launch_server "$engram"
  if wait_for_listener; then return 0; fi

  tail_text="$(tail -n 8 "$ABLIT_STATE_DIR/logs/engine.log" 2>/dev/null | tr '\n' ' ' || true)"
  if ! ablit_step_done extract && [ "$ENGRAM_MODE" = "direct" ] \
      && { engram_open_failed_since "$mark_engine" || engram_open_failed_since "$mark_core"; }; then
    # The engine could not read the Engram files where they are. Extract them and
    # try once more. The extraction fails the run if the disk is too small.
    ablit_log "the engine could not read the Engram files in place, so it falls back to extraction"
    run_extraction
    engram="$(engram_argument)"
    ablit_log "starting the engine again, Engram files from $engram"
    launch_server "$engram"
    wait_for_listener || ablit_fail "engine_exited" "the engine ended after extraction: $(tail -n 8 "$ABLIT_STATE_DIR/logs/engine.log" | tr '\n' ' ')"
    return 0
  fi
  ablit_fail "engine_exited" "the engine ended before it opened its port: $tail_text"
}

stage_health()  { ablit_health_check; }
stage_ready()   { ablit_write_ready; }

# A command failed on its own. A step that failed on purpose has already written
# failed.json with the real reason, and that file must stay.
bootstrap_error() {
  [ ! -f "$ABLIT_STATE_DIR/failed.json" ] || exit "$BOOTSTRAP_FAILED"
  ablit_fail command_failed "A bootstrap command failed."
}

main() {
  trap bootstrap_error ERR
  # Stop the background steps when the script ends for any reason.
  trap ablit_kill_jobs EXIT
  # A process can be gone after a container restart even when its stage finished.
  if [ ! -f "$ABLIT_STATE_DIR/engine.pid" ] || ! ablit_pid_alive "$(cat "$ABLIT_STATE_DIR/engine.pid")"; then
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

# Tests source this file to call single functions. Running it starts the stages.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
