#!/usr/bin/env bash
# Shared functions for the engine bootstrap and health scripts.
#
# This file is sourced, not run. The engine scripts set `set -euo pipefail`
# before they source it. On the rented machine the CLI copies this file to
# /ablit/engine/common.sh and sets ABLIT_LIB to that path.
#
# Every step that needs a real rented machine is marked
#   untested on a rented machine
# because no script here has run on a rented box yet.
#
# Inputs are environment variables, listed under `variables` in each engine's
# lock.yaml. The endpoint key arrives only as ABLIT_ENDPOINT_KEY. It is never
# written to a file, put in a command line argument, or printed.

ABLIT_STATE_DIR="${ABLIT_STATE_DIR:-/ablit}"
ABLIT_DATA_DIR="${ABLIT_DATA_DIR:-/models}"
ABLIT_ARTIFACTS_TSV="${ABLIT_ARTIFACTS_TSV:-$ABLIT_STATE_DIR/artifacts.tsv}"
ABLIT_WATCHDOG="${ABLIT_WATCHDOG:-$ABLIT_STATE_DIR/ablit_watchdog.py}"
ABLIT_SPEED_FLOOR_PERCENT="${ABLIT_SPEED_FLOOR_PERCENT:-90}"
ABLIT_SPEED_TEST_SECONDS="${ABLIT_SPEED_TEST_SECONDS:-120}"

# The download helper sits beside this file, both in the repository (engines/lib)
# and on the rented machine (/ablit/engine). ABLIT_FETCH overrides the path.
case "${BASH_SOURCE[0]}" in
  */*) _ablit_lib_dir="${BASH_SOURCE[0]%/*}" ;;
  *) _ablit_lib_dir="." ;;
esac
ABLIT_FETCH="${ABLIT_FETCH:-$_ablit_lib_dir/fetch.py}"

BOOTSTRAP_FAILED=6   # exit code for a failed bootstrap, speed check or health check

ablit_log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

# Succeeds when the process exists and has not exited. A process that has exited
# but has not been collected by its parent still answers kill -0. That happens to
# an engine that was started by an earlier run of this script, because its parent
# is then the container's first process, and that process may never collect it.
# Where /proc exists, the process state tells the two cases apart.
ablit_pid_alive() {
  local pid="${1:-}" line
  # Pid 0 would signal the whole process group, so it counts as no process.
  case "$pid" in ''|0|*[!0-9]*) return 1 ;; esac
  kill -0 "$pid" 2>/dev/null || return 1
  [ -r /proc/self/status ] || return 0
  # A missing status file means the process is gone.
  [ -r "/proc/$pid/status" ] || return 1
  while IFS= read -r line; do
    case "$line" in
      "State:"*)
        case "$line" in *Z*) return 1 ;; esac
        break
        ;;
    esac
  done <"/proc/$pid/status" 2>/dev/null
  return 0
}

# Record why the bootstrap failed, then exit with code 6.
# The CLI reads failed.json and decides what to destroy. The first failure stays:
# a step that stops because another step failed must not hide the real reason.
ablit_fail() {
  local reason="$1" detail="${2:-}"
  ablit_log "FAILED: $reason $detail"
  mkdir -p "$ABLIT_STATE_DIR"
  if [ ! -f "$ABLIT_STATE_DIR/failed.json" ]; then
    # shellcheck disable=SC1003
    printf '{"reason":"%s","detail":"%s","at":%s}\n' \
      "$reason" "$(printf '%s' "$detail" | tr -d '"\\' | tr '\n' ' ')" "$(date +%s)" \
      >"$ABLIT_STATE_DIR/failed.json"
  fi
  exit "$BOOTSTRAP_FAILED"
}

ablit_require_env() {
  local name
  for name in "$@"; do
    if [ -z "${!name:-}" ]; then
      ablit_fail "missing_variable" "$name is not set"
    fi
  done
}

ablit_is_number() {
  case "$1" in
    ''|*[!0-9.]*|*.*.*) return 1 ;;
    *) return 0 ;;
  esac
}

# --- stage markers ---------------------------------------------------------
# Each finished stage writes $ABLIT_STATE_DIR/stages/<NN>-<name>.done, so a
# restart skips the stages that already finished.

ablit_stage_marker() { printf '%s/stages/%s-%s.done' "$ABLIT_STATE_DIR" "$1" "$2"; }

# usage: ablit_run_stage 02 check function_name
ablit_run_stage() {
  local number="$1" name="$2" fn="$3" marker
  marker="$(ablit_stage_marker "$number" "$name")"
  if [ -f "$marker" ]; then
    ablit_log "stage $number $name: already done, skipping"
    return 0
  fi
  ablit_log "stage $number $name: starting"
  "$fn"
  mkdir -p "$ABLIT_STATE_DIR/stages"
  date +%s >"$marker"
  ablit_log "stage $number $name: done"
}

# --- stage 1: watchdog and run file ----------------------------------------

# Write /ablit/run.json. The watchdog reads it. All values must be numbers,
# except the two IDs, which are checked against a short character set.
ablit_write_run_file() {
  # Never rewrite it. A restart must keep the original start time and baseline.
  if [ -f "$ABLIT_STATE_DIR/run.json" ]; then
    return 0
  fi
  ablit_require_env ABLIT_RUN_ID ABLIT_MODEL_ID ABLIT_CAP_USD ABLIT_HOURLY_RATE \
    ABLIT_TRANSFER_PER_GB ABLIT_ADVERTISED_MBPS
  local name
  for name in ABLIT_CAP_USD ABLIT_HOURLY_RATE ABLIT_TRANSFER_PER_GB ABLIT_ADVERTISED_MBPS; do
    ablit_is_number "${!name}" || ablit_fail "bad_variable" "$name is not a number"
  done
  case "$ABLIT_RUN_ID$ABLIT_MODEL_ID" in
    *[!A-Za-z0-9._-]*) ablit_fail "bad_variable" "run and model IDs may only hold letters, digits, dot, dash and underscore" ;;
  esac
  local started rx
  started="${ABLIT_STARTED_AT:-$(date +%s)}"
  rx="$(awk -F'[: ]+' 'NR>2 && $2!="lo" {sum+=$3} END {printf "%d", sum}' /proc/net/dev)"
  mkdir -p "$ABLIT_STATE_DIR/logs" "$ABLIT_STATE_DIR/stages"
  printf '{"run_id":"%s","model_id":"%s","started_at":%s,"hourly_rate":%s,"transfer_price_per_gb":%s,"cap_usd":%s,"advertised_mbps":%s,"baseline_rx_bytes":%s}\n' \
    "$ABLIT_RUN_ID" "$ABLIT_MODEL_ID" "$started" "$ABLIT_HOURLY_RATE" "$ABLIT_TRANSFER_PER_GB" \
    "$ABLIT_CAP_USD" "$ABLIT_ADVERTISED_MBPS" "${rx:-0}" >"$ABLIT_STATE_DIR/run.json"
}

# Start the watchdog before any download. Safe to call again after a restart.
# untested on a rented machine. Needs python3 on the box.
ablit_start_watchdog() {
  local pidfile="$ABLIT_STATE_DIR/watchdog.pid"
  if [ -f "$pidfile" ] && ablit_pid_alive "$(cat "$pidfile")"; then
    return 0
  fi
  command -v python3 >/dev/null 2>&1 || ablit_fail "missing_tool" "python3 is needed for the watchdog"
  [ -f "$ABLIT_WATCHDOG" ] || ablit_fail "missing_file" "$ABLIT_WATCHDOG was not copied to the box"
  nohup python3 "$ABLIT_WATCHDOG" --config "$ABLIT_STATE_DIR/run.json" \
    >>"$ABLIT_STATE_DIR/logs/watchdog.log" 2>&1 &
  echo $! >"$pidfile"
  ablit_log "watchdog started"
}

# --- stage 2: hardware checks ----------------------------------------------

# Compare two decimal numbers. Succeeds when $1 >= $2.
ablit_ge() { awk -v a="$1" -v b="$2" 'BEGIN { exit !(a + 0 >= b + 0) }'; }

# Succeeds when a compute capability such as 8.6 is in ABLIT_CUDA_ARCHITECTURES,
# a list of the same numbers times ten (for example "86 89 120").
ablit_arch_listed() {
  awk -v cc="$1" -v list="$ABLIT_CUDA_ARCHITECTURES" 'BEGIN {
    n = split(list, a, /[ ,]+/); v = sprintf("%.0f", cc * 10)
    for (i = 1; i <= n; i++) if (a[i] == v) exit 0
    exit 1 }'
}

# untested on a rented machine. Needs nvidia-smi and the cgroup files of a real container.
ablit_check_hardware() {
  ablit_require_env ABLIT_GPU_COUNT ABLIT_MIN_VRAM_MB_EACH ABLIT_MIN_HOST_RAM_GB ABLIT_MIN_DISK_GB
  command -v nvidia-smi >/dev/null 2>&1 || ablit_fail "missing_tool" "nvidia-smi was not found"

  local rows count
  rows="$(nvidia-smi --query-gpu=memory.total,compute_cap --format=csv,noheader,nounits)"
  count="$(printf '%s\n' "$rows" | grep -c .)"
  [ "$count" -eq "$ABLIT_GPU_COUNT" ] || ablit_fail "gpu_count" "found $count GPUs, the profile needs $ABLIT_GPU_COUNT"

  local mem cc
  while IFS=', ' read -r mem cc; do
    ablit_ge "$mem" "$ABLIT_MIN_VRAM_MB_EACH" || ablit_fail "gpu_vram" "a GPU has $mem MiB, the profile needs $ABLIT_MIN_VRAM_MB_EACH MiB"
    if [ -n "${ABLIT_MIN_CC:-}" ]; then
      ablit_ge "$cc" "$ABLIT_MIN_CC" || ablit_fail "gpu_compute_capability" "a GPU reports $cc, the profile needs $ABLIT_MIN_CC"
    fi
    # An engine image holds machine code for named GPU generations only.
    if [ -n "${ABLIT_CUDA_ARCHITECTURES:-}" ] && ! ablit_arch_listed "$cc"; then
      ablit_fail "gpu_generation" "a GPU reports compute capability $cc, the engine image holds code for ${ABLIT_CUDA_ARCHITECTURES}"
    fi
  done <<<"$rows"

  # The NVIDIA driver must be new enough for the CUDA version of the image.
  if [ -n "${ABLIT_MIN_DRIVER_MAJOR:-}" ]; then
    local driver
    driver="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n 1 | tr -d ' ')"
    ablit_is_number "${driver%%.*}" || ablit_fail "driver_version" "nvidia-smi gave no driver version"
    [ "${driver%%.*}" -ge "$ABLIT_MIN_DRIVER_MAJOR" ] \
      || ablit_fail "driver_too_old" "the NVIDIA driver is $driver, the engine image needs ${ABLIT_MIN_DRIVER_MAJOR} or newer"
  fi

  # Memory the container may use: the smaller of the cgroup limit and MemTotal.
  local meminfo_kb limit_bytes="" mem_bytes
  meminfo_kb="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
  mem_bytes=$((meminfo_kb * 1024))
  if [ -r /sys/fs/cgroup/memory.max ]; then
    limit_bytes="$(cat /sys/fs/cgroup/memory.max)"
  elif [ -r /sys/fs/cgroup/memory/memory.limit_in_bytes ]; then
    limit_bytes="$(cat /sys/fs/cgroup/memory/memory.limit_in_bytes)"
  fi
  if ablit_is_number "${limit_bytes:-x}" && [ "$limit_bytes" -lt "$mem_bytes" ]; then
    mem_bytes="$limit_bytes"
  fi
  ablit_ge "$mem_bytes" "$(awk -v g="$ABLIT_MIN_HOST_RAM_GB" 'BEGIN {printf "%.0f", g * 1e9}')" \
    || ablit_fail "host_ram" "usable memory is $mem_bytes bytes, the profile needs ${ABLIT_MIN_HOST_RAM_GB} GB"

  # Locked-memory limit. The engine template sets ABLIT_MIN_MEMLOCK_KB when it pins memory.
  # A low limit is logged, not fatal. Vast containers accept no --ulimit option
  # (https://docs.vast.ai/guides/instances/docker-environment.md), and the engines
  # pin with cudaHostRegister and fall back to smaller or unpinned buffers when
  # pinning is refused, which is slower but runs.
  if [ -n "${ABLIT_MIN_MEMLOCK_KB:-}" ]; then
    local memlock
    memlock="$(ulimit -l)"
    if [ "$memlock" != "unlimited" ] && ! ablit_ge "$memlock" "$ABLIT_MIN_MEMLOCK_KB"; then
      ablit_log "warning: ulimit -l is $memlock KB and the engine asks for $ABLIT_MIN_MEMLOCK_KB KB, so pinning may fall back"
    fi
  fi

  mkdir -p "$ABLIT_DATA_DIR"
  local avail
  avail="$(df -B1 --output=avail "$ABLIT_DATA_DIR" | tail -n 1 | tr -d ' ')"
  ablit_ge "$avail" "$(awk -v g="$ABLIT_MIN_DISK_GB" 'BEGIN {printf "%.0f", g * 1e9}')" \
    || ablit_fail "disk" "$avail bytes are free on $ABLIT_DATA_DIR, the profile needs ${ABLIT_MIN_DISK_GB} GB"
}

# --- stage 3: random-read test ---------------------------------------------

# Bounded random 4 KiB reads on a scratch file with the page cache bypassed.
# The result is reported and never fails the run. A pass threshold is set only
# after a measured passing box.
# untested on a rented machine. O_DIRECT may be refused on some filesystems.
ablit_random_read_test() {
  if [ "${ABLIT_RANDOM_READ_REQUIRED:-0}" != "1" ]; then
    ablit_log "random-read test not required by this profile"
    return 0
  fi
  local scratch="$ABLIT_DATA_DIR/.ablit-randread.bin"
  dd if=/dev/zero of="$scratch" bs=1M count=2048 conv=fsync status=none
  python3 - "$scratch" "$ABLIT_STATE_DIR/randread.json" <<'PY' || ablit_log "random-read test could not run"
import json, mmap, os, random, sys, time

path, out = sys.argv[1], sys.argv[2]
size = os.path.getsize(path)
blocks = size // 4096
buffer = mmap.mmap(-1, 4096)
direct = True
try:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECT)
except OSError:
    fd = os.open(path, os.O_RDONLY)
    direct = False
rng = random.Random(1)
reads, deadline, start = 0, time.monotonic() + 10.0, time.monotonic()
while time.monotonic() < deadline and reads < 50000:
    os.preadv(fd, [buffer], rng.randrange(blocks) * 4096)
    reads += 1
elapsed = time.monotonic() - start
os.close(fd)
result = {"reads": reads, "seconds": round(elapsed, 3), "iops": round(reads / elapsed), "page_cache_bypassed": direct}
print(json.dumps(result), file=sys.stderr)
with open(out, "w") as handle:
    json.dump(result, handle)
PY
  rm -f "$scratch"
}

# --- artifact list ---------------------------------------------------------
# $ABLIT_ARTIFACTS_TSV has one line per file, with tab-separated columns:
#   url  destination (relative to the data dir)  bytes  sha256  role

# Print the absolute paths of all artifacts with a role, sorted by name.
ablit_paths_by_role() {
  awk -F'\t' -v role="$1" -v root="$ABLIT_DATA_DIR" '$5 == role {print root "/" $2}' "$ABLIT_ARTIFACTS_TSV" | sort
}

# Print the paths relative to the data directory, sorted by name.
ablit_rels_by_role() {
  awk -F'\t' -v role="$1" '$5 == role {print $2}' "$ABLIT_ARTIFACTS_TSV" | sort
}

# Print the total size in bytes of all artifacts with a role.
ablit_bytes_by_role() {
  awk -F'\t' -v role="$1" '$5 == role {sum += $3} END {printf "%.0f\n", sum}' "$ABLIT_ARTIFACTS_TSV"
}

# --- downloads -------------------------------------------------------------
# The work is done by fetch.py (standard library Python), which downloads several
# files at once, cuts big files into byte ranges fetched in parallel, resumes
# each range after a stop, hashes each file while it downloads, and renames a
# file to its final name only after its size and SHA-256 match. Settings are the
# ABLIT_DOWNLOAD_* variables described at the top of fetch.py.
#
# fetch.py prints one result line. This function turns it into a return value, or
# into a failure with the reason it gives (see ablit_fail).
#   ABLIT_FETCH_STATUS   OK, or LIMIT when a time limit ended the run
#   ABLIT_FETCH_BYTES    bytes downloaded by this call (resumed bytes do not count)
#   ABLIT_FETCH_SECONDS  seconds this call took
#   ABLIT_FETCH_MBPS     speed over the last ten seconds of the call
_ablit_fetch() {
  command -v python3 >/dev/null 2>&1 || ablit_fail "missing_tool" "python3 is needed for downloads"
  [ -f "$ABLIT_FETCH" ] || ablit_fail "missing_file" "$ABLIT_FETCH was not copied to the box"
  local out="" status=0 kind="" first="" second="" third=""
  # Bash 3.2 can run an inherited ERR trap inside this substitution even when
  # the caller handles its status. The helper's FAIL line supplies the reason.
  out="$(trap - ERR; python3 "$ABLIT_FETCH" "$@")" || status=$?
  IFS=$'\t' read -r kind first second third <<<"$(printf '%s\n' "$out" | tail -n 1)"
  case "$kind" in
    OK|LIMIT)
      # shellcheck disable=SC2034
      ABLIT_FETCH_STATUS="$kind"
      ABLIT_FETCH_BYTES="$first"
      ABLIT_FETCH_SECONDS="$second"
      ABLIT_FETCH_MBPS="${third:-0}"
      ;;
    FAIL) ablit_fail "${first:-download_failed}" "$second" ;;
    *) ablit_fail "download_failed" "the download helper ended with code $status and gave no result" ;;
  esac
}

# Join roles with commas for the helper: ablit_roles_arg role...
_ablit_roles() { (IFS=,; printf '%s' "$*"); }

# --- stage 4: download speed check -----------------------------------------

# Measure the speed of the real download, with the same parallel connections the
# download stage uses. The helper runs for at most ABLIT_SPEED_TEST_SECONDS and
# keeps what it fetched, so the next stage continues from there. A single
# connection would measure the speed of one connection and fail a fast machine.
# Optional arguments are roles. They choose and order the files for the test.
# The measured speed is the better of two figures: the average over the whole
# test, and the speed over its last ten seconds. The average includes the first
# seconds, when the connections are still speeding up, so on its own it would
# fail a fast machine on a short test.
# untested on a rented machine.
ablit_speed_check() {
  ablit_require_env ABLIT_MIN_DOWNLOAD_MBPS
  local roles
  roles="$(_ablit_roles "$@")"
  _ablit_fetch all --tsv "$ABLIT_ARTIFACTS_TSV" --root "$ABLIT_DATA_DIR" --state "$ABLIT_STATE_DIR" \
    --time-limit "$ABLIT_SPEED_TEST_SECONDS" ${roles:+--roles "$roles"}
  if [ "${ABLIT_FETCH_BYTES:-0}" = "0" ]; then
    ablit_log "no data was left to download, so there is no speed to measure"
    return 0
  fi
  local mbps floor
  mbps="$(awk -v b="$ABLIT_FETCH_BYTES" -v t="$ABLIT_FETCH_SECONDS" -v r="${ABLIT_FETCH_MBPS:-0}" \
    'BEGIN {a = b * 8 / 1e6 / (t > 0 ? t : 1e-6); printf "%.1f", (r + 0 > a ? r + 0 : a)}')"
  floor="$(awk -v f="$ABLIT_MIN_DOWNLOAD_MBPS" -v p="$ABLIT_SPEED_FLOOR_PERCENT" 'BEGIN {printf "%.1f", f * p / 100}')"
  ablit_log "measured download speed ${mbps} Mbps, the run fails below ${floor} Mbps"
  printf '{"measured_mbps":%s,"floor_mbps":%s,"seconds":%s}\n' "$mbps" "$ABLIT_MIN_DOWNLOAD_MBPS" "$ABLIT_FETCH_SECONDS" \
    >"$ABLIT_STATE_DIR/speed.json"
  if ! ablit_ge "$mbps" "$floor"; then
    # The CLI destroys the run and adds this host to the local deny list.
    # It never picks a replacement host by itself.
    ablit_fail "speed_below_floor" "measured ${mbps} Mbps, floor ${floor} Mbps"
  fi
}

# --- stage 5: download -----------------------------------------------------

# Download one file with parallel range requests, then check size and hash before
# the rename. A complete .part file is checked and renamed without a request.
# untested on a rented machine.
ablit_download_one() {
  local url="$1" rel="$2" want_bytes="$3" want_sha="$4"
  _ablit_fetch one --url "$url" --rel "$rel" --bytes "$want_bytes" --sha256 "$want_sha" \
    --root "$ABLIT_DATA_DIR" --state "$ABLIT_STATE_DIR"
}

# Download every file in the artifact list, in list order. Optional arguments are
# roles. With roles, only the files with those roles are downloaded, in the order
# of the roles, and progress still counts the whole list. A file that an earlier
# call already verified is skipped without reading it again.
# untested on a rented machine.
ablit_download_all() {
  local roles
  roles="$(_ablit_roles "$@")"
  _ablit_fetch all --tsv "$ABLIT_ARTIFACTS_TSV" --root "$ABLIT_DATA_DIR" --state "$ABLIT_STATE_DIR" \
    ${roles:+--roles "$roles"}
}

# Remember that a downloaded file was used up and may be gone, so a restart does
# not download it again.
ablit_mark_consumed() {
  mkdir -p "$ABLIT_STATE_DIR/consumed"
  : >"$ABLIT_STATE_DIR/consumed/${1//\//%2F}"
}

# Remove a downloaded file that a later step has used up, and remember that.
ablit_consume() {
  ablit_mark_consumed "$1"
  rm -f "$ABLIT_DATA_DIR/$1"
}

# --- background steps ------------------------------------------------------
# Some bootstraps run steps while the download is still going. A background step
# that fails writes failed.json itself (ablit_fail) and exits with code 6. The
# main script has to notice that, so it starts steps with ablit_spawn and waits
# for them with ablit_wait_jobs or ablit_wait_for_files.

ABLIT_JOB_PIDS=()
ABLIT_JOB_NAMES=()

# usage: ablit_spawn name command args...
ablit_spawn() {
  local name="$1"
  shift
  ( "$@" ) &
  ABLIT_JOB_PIDS+=("$!")
  ABLIT_JOB_NAMES+=("$name")
  ablit_log "started background step $name"
}

# Exit with code 6 when any background step has ended with an error.
_ablit_check_jobs() {
  local index=0 count="${#ABLIT_JOB_PIDS[@]}" pid status
  while [ "$index" -lt "$count" ]; do
    pid="${ABLIT_JOB_PIDS[$index]}"
    if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
      status=0
      wait "$pid" || status=$?
      ABLIT_JOB_PIDS[index]=""
      if [ "$status" -ne 0 ]; then
        [ -f "$ABLIT_STATE_DIR/failed.json" ] \
          || ablit_fail "background_step_failed" "${ABLIT_JOB_NAMES[$index]} ended with code $status"
        exit "$BOOTSTRAP_FAILED"
      fi
    fi
    index=$((index + 1))
  done
}

# Wait for background steps to end, and stop at once if any step has failed.
# With names, wait for those steps only. Without names, wait for all of them.
# usage: ablit_wait_jobs [name...]
ablit_wait_jobs() {
  local want=("$@") index pending
  while :; do
    _ablit_check_jobs
    pending=0
    index=0
    while [ "$index" -lt "${#ABLIT_JOB_PIDS[@]}" ]; do
      if [ -n "${ABLIT_JOB_PIDS[$index]}" ]; then
        if [ "${#want[@]}" -eq 0 ]; then
          pending=1
        else
          local name
          for name in "${want[@]}"; do
            [ "$name" != "${ABLIT_JOB_NAMES[$index]}" ] || pending=1
          done
        fi
      fi
      index=$((index + 1))
    done
    [ "$pending" = 1 ] || return 0
    sleep "${ABLIT_WAIT_POLL_S:-1}"
  done
}

# Wait until all the given files exist. Used by a background step that needs a
# download to finish first. It stops when the run has failed.
ablit_wait_for_files() {
  local file
  for file in "$@"; do
    while [ ! -e "$file" ]; do
      [ ! -f "$ABLIT_STATE_DIR/failed.json" ] || exit "$BOOTSTRAP_FAILED"
      sleep "${ABLIT_WAIT_POLL_S:-1}"
    done
  done
}

# Print the process IDs of everything below a process, children before
# grandchildren. It reads /proc, because the Xolotl image has no ps or pkill.
# Where there is no /proc (a developer machine), it asks pgrep instead.
_ablit_descendants() {
  local stat line rest pid ppid
  local -a pids=() ppids=() frontier=("$1") next=()
  if [ ! -r /proc/self/stat ]; then
    local parent child
    while [ "${#frontier[@]}" -gt 0 ]; do
      next=()
      for parent in "${frontier[@]}"; do
        for child in $(pgrep -P "$parent" 2>/dev/null || true); do
          printf '%s\n' "$child"
          next+=("$child")
        done
      done
      frontier=("${next[@]+"${next[@]}"}")
    done
    return 0
  fi
  for stat in /proc/[0-9]*/stat; do
    line=""
    { read -r line <"$stat"; } 2>/dev/null || continue
    [ -n "$line" ] || continue
    pid="${stat#/proc/}"
    pid="${pid%/stat}"
    rest="${line##*) }"
    # shellcheck disable=SC2086
    set -- $rest
    ppid="${2:-}"
    [ -n "$ppid" ] || continue
    pids+=("$pid")
    ppids+=("$ppid")
  done
  local round=0 current index
  while [ "${#frontier[@]}" -gt 0 ] && [ "$round" -lt 16 ]; do
    next=()
    for current in "${frontier[@]}"; do
      index=0
      while [ "$index" -lt "${#pids[@]}" ]; do
        if [ "${ppids[$index]}" = "$current" ]; then
          printf '%s\n' "${pids[$index]}"
          next+=("${pids[$index]}")
        fi
        index=$((index + 1))
      done
    done
    frontier=("${next[@]+"${next[@]}"}")
    round=$((round + 1))
  done
}

# Stop the background steps and what they started. Use it as an EXIT trap. A step
# that already ended is skipped.
ablit_kill_jobs() {
  local pid child
  for pid in ${ABLIT_JOB_PIDS[@]+"${ABLIT_JOB_PIDS[@]}"}; do
    [ -n "$pid" ] || continue
    kill -0 "$pid" 2>/dev/null || continue
    for child in $(_ablit_descendants "$pid"); do
      kill -TERM "$child" 2>/dev/null || true
    done
    kill -TERM "$pid" 2>/dev/null || true
  done
  return 0
}

# A finished sub-step leaves a marker, so a restart skips it. These markers are
# not stage markers: the CLI only knows the nine stages.
ablit_step_done() { [ -f "$ABLIT_STATE_DIR/steps/$1.done" ]; }
ablit_step_mark() {
  mkdir -p "$ABLIT_STATE_DIR/steps"
  date +%s >"$ABLIT_STATE_DIR/steps/$1.done"
}

# Append finished files to one output file in the given order, as each file
# appears, and optionally delete each file after it was appended.
# usage: ablit_join_parts OUT_REL EXPECT_BYTES DELETE(0 or 1) PART_REL...
# untested on a rented machine.
ablit_join_parts() {
  local out="$1" expect="$2" delete="$3" flag=()
  shift 3
  [ "$delete" != 1 ] || flag=(--delete-inputs)
  _ablit_fetch join --root "$ABLIT_DATA_DIR" --state "$ABLIT_STATE_DIR" --out "$out" \
    --expect-bytes "$expect" --watch-pid "$$" ${flag[@]+"${flag[@]}"} "$@"
}

# --- stage 8: health check -------------------------------------------------

# Succeeds when the status code of the last request is one of the allowed codes.
_ablit_http_code() {
  # usage: _ablit_http_code <curl args...>; prints the status code
  curl --silent --output /dev/null --write-out '%{http_code}' --max-time 60 "$@" || true
}

# untested on a rented machine.
ablit_health_check() {
  ablit_require_env ABLIT_PORT ABLIT_ENDPOINT_KEY ABLIT_SERVED_NAME
  case "$ABLIT_SERVED_NAME" in
    *[!A-Za-z0-9._-]*) ablit_fail "bad_variable" "ABLIT_SERVED_NAME holds characters outside letters, digits, dot, dash and underscore" ;;
  esac
  local base="http://127.0.0.1:${ABLIT_PORT}"
  local timeout="${ABLIT_READY_TIMEOUT_S:-900}" waited=0 code poll="${ABLIT_HEALTH_POLL_S:-5}"

  # 1. GET /v1/models with the key, until the engine answers or the timeout passes.
  while :; do
    code="$(_ablit_http_code -H "Authorization: Bearer ${ABLIT_ENDPOINT_KEY}" "$base/v1/models")"
    [ "$code" = "200" ] && break
    # An engine that has died will never answer. Fail now, not after the timeout.
    if [ -f "$ABLIT_STATE_DIR/engine.pid" ] && ! ablit_pid_alive "$(cat "$ABLIT_STATE_DIR/engine.pid")"; then
      ablit_fail "engine_exited" "the engine ended before it answered: $(tail -n 5 "$ABLIT_STATE_DIR/logs/engine.log" 2>/dev/null | tr '\n' ' ')"
    fi
    waited=$((waited + poll))
    [ "$waited" -lt "$timeout" ] || ablit_fail "health_timeout" "GET /v1/models returned $code after ${timeout}s"
    sleep "${ABLIT_HEALTH_SLEEP_S:-$poll}"
  done

  # The engine must listen on the loopback address only.
  python3 - "$ABLIT_PORT" <<'PYBIND' || ablit_fail "bind_check" "the engine must listen on loopback only"
import sys
port = int(sys.argv[1])
found = False
for path in ("/proc/net/tcp", "/proc/net/tcp6"):
    with open(path) as handle:
        for line in handle.readlines()[1:]:
            fields = line.split()
            address, value = fields[1].split(":")
            if int(value, 16) != port or fields[3] != "0A":
                continue
            found = True
            if address not in ("0100007F", "00000000000000000000000001000000"):
                sys.exit(1)
sys.exit(0 if found else 1)
PYBIND

  # 2. One chat completion with max_tokens 1.
  local body
  body="$(printf '{"model":"%s","messages":[{"role":"user","content":"Hello"}],"max_tokens":1}' "$ABLIT_SERVED_NAME")"
  code="$(_ablit_http_code -H "Authorization: Bearer ${ABLIT_ENDPOINT_KEY}" -H 'Content-Type: application/json' \
    -d "$body" "$base/v1/chat/completions")"
  [ "$code" = "200" ] || ablit_fail "health_chat" "chat completion returned $code"

  # 3. One streamed completion. It must send data lines and end with [DONE].
  local stream
  stream="$(printf '{"model":"%s","messages":[{"role":"user","content":"Hello"}],"max_tokens":4,"stream":true}' "$ABLIT_SERVED_NAME")"
  curl --silent --no-buffer --max-time 120 -H "Authorization: Bearer ${ABLIT_ENDPOINT_KEY}" \
    -H 'Content-Type: application/json' -d "$stream" "$base/v1/chat/completions" \
    >"$ABLIT_STATE_DIR/stream-check.txt" || true
  grep -q '^data: ' "$ABLIT_STATE_DIR/stream-check.txt" || ablit_fail "health_stream" "no data lines in the streamed reply"
  grep -q '\[DONE\]' "$ABLIT_STATE_DIR/stream-check.txt" || ablit_fail "health_stream" "the streamed reply did not end with [DONE]"
  rm -f "$ABLIT_STATE_DIR/stream-check.txt"

  # 4. Requests with no key, or a wrong key, must be refused.
  code="$(_ablit_http_code "$base/v1/models")"
  case "$code" in 401|403) ;; *) ablit_fail "health_auth" "a request with no key returned $code" ;; esac
  code="$(_ablit_http_code -H "Authorization: Bearer wrong-${ABLIT_RUN_ID:-key}-wrong" "$base/v1/models")"
  case "$code" in 401|403) ;; *) ablit_fail "health_auth" "a request with a wrong key returned $code" ;; esac
}

# --- stage 9: ready --------------------------------------------------------

ablit_write_ready() {
  printf '{"ready_at":%s,"port":%s,"served_name":"%s","model_id":"%s"}\n' \
    "$(date +%s)" "$ABLIT_PORT" "$ABLIT_SERVED_NAME" "${ABLIT_MODEL_ID:-}" >"$ABLIT_STATE_DIR/ready.json"
}

# Start a command in the background with the endpoint key in its environment
# only, log to $ABLIT_STATE_DIR/logs/engine.log, and remember the process ID in
# ABLIT_ENGINE_PID and in $ABLIT_STATE_DIR/engine.pid. It checks nothing after
# the start, so a caller can decide for itself what a quick exit means.
# usage: ablit_launch_engine KEY_VARIABLE_NAME command args...
ablit_launch_engine() {
  local key_var="$1"
  shift
  mkdir -p "$ABLIT_STATE_DIR/logs"
  # The key goes into the child's environment through a builtin export in a
  # subshell, so it never appears in a command line that `ps` can show.
  (
    export "${key_var}=${ABLIT_ENDPOINT_KEY}"
    exec nohup "$@"
  ) >>"$ABLIT_STATE_DIR/logs/engine.log" 2>&1 &
  ABLIT_ENGINE_PID=$!
  echo "$ABLIT_ENGINE_PID" >"$ABLIT_STATE_DIR/engine.pid"
}

# Launch the engine, then fail with the end of its log when it exits within a few
# seconds. An engine that cannot start usually exits at once, and this fails then
# instead of waiting for the health check to time out.
# usage: ablit_start_engine KEY_VARIABLE_NAME command args...
ablit_start_engine() {
  ablit_launch_engine "$@"
  sleep "${ABLIT_ENGINE_START_CHECK_S:-10}"
  if ! ablit_pid_alive "$ABLIT_ENGINE_PID"; then
    ablit_fail "engine_exited" "the engine exited on start: $(tail -n 5 "$ABLIT_STATE_DIR/logs/engine.log" | tr '\n' ' ')"
  fi
}

# Succeeds when something accepts connections on 127.0.0.1 at the port.
ablit_port_open() {
  (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

# Print the free bytes on the file system that holds a directory.
ablit_free_bytes() {
  df -B1 --output=avail "$1" | tail -n 1 | tr -d ' '
}
