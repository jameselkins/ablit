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

BOOTSTRAP_FAILED=6   # exit code for a failed bootstrap, speed check or health check

ablit_log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

# Record why the bootstrap failed, then exit with code 6.
# The CLI reads failed.json and decides what to destroy.
ablit_fail() {
  local reason="$1" detail="${2:-}"
  ablit_log "FAILED: $reason $detail"
  mkdir -p "$ABLIT_STATE_DIR"
  # shellcheck disable=SC1003
  printf '{"reason":"%s","detail":"%s","at":%s}\n' \
    "$reason" "$(printf '%s' "$detail" | tr -d '"\\' | tr '\n' ' ')" "$(date +%s)" \
    >"$ABLIT_STATE_DIR/failed.json"
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
  if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
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
  done <<<"$rows"

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
  if [ -n "${ABLIT_MIN_MEMLOCK_KB:-}" ]; then
    local memlock
    memlock="$(ulimit -l)"
    if [ "$memlock" != "unlimited" ]; then
      ablit_ge "$memlock" "$ABLIT_MIN_MEMLOCK_KB" \
        || ablit_fail "memlock" "ulimit -l is $memlock KB, the engine needs $ABLIT_MIN_MEMLOCK_KB KB"
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

# --- stage 4: download speed check -----------------------------------------

ablit_file_size() { stat -c %s "$1" 2>/dev/null || echo 0; }

# Measure speed over the first two minutes of the largest file. The partial file
# is kept, so the next stage resumes from it.
# untested on a rented machine.
ablit_speed_check() {
  ablit_require_env ABLIT_MIN_DOWNLOAD_MBPS
  local line url dest
  line="$(sort -t$'\t' -k3,3nr "$ABLIT_ARTIFACTS_TSV" | head -n 1)"
  url="$(printf '%s' "$line" | cut -f1)"
  dest="$ABLIT_DATA_DIR/$(printf '%s' "$line" | cut -f2)"
  mkdir -p "$(dirname "$dest")"

  local before after started elapsed mbps floor
  before="$(ablit_file_size "$dest.part")"
  started="$(date +%s.%N)"
  # A timeout (exit 28) is expected: it ends the measurement.
  curl --silent --show-error --location --continue-at - --max-time "$ABLIT_SPEED_TEST_SECONDS" \
    --output "$dest.part" "$url" </dev/null || true
  elapsed="$(awk -v s="$started" -v e="$(date +%s.%N)" 'BEGIN {print e - s}')"
  after="$(ablit_file_size "$dest.part")"
  mbps="$(awk -v b="$((after - before))" -v t="$elapsed" 'BEGIN {printf "%.1f", b * 8 / 1e6 / t}')"
  floor="$(awk -v f="$ABLIT_MIN_DOWNLOAD_MBPS" -v p="$ABLIT_SPEED_FLOOR_PERCENT" 'BEGIN {printf "%.1f", f * p / 100}')"
  ablit_log "measured download speed ${mbps} Mbps, the run fails below ${floor} Mbps"
  printf '{"measured_mbps":%s,"floor_mbps":%s,"seconds":%s}\n' "$mbps" "$ABLIT_MIN_DOWNLOAD_MBPS" "$elapsed" \
    >"$ABLIT_STATE_DIR/speed.json"
  if ! ablit_ge "$mbps" "$floor"; then
    # The CLI destroys the run and adds this host to the local deny list.
    # It never picks a replacement host by itself.
    ablit_fail "speed_below_floor" "measured ${mbps} Mbps, floor ${floor} Mbps"
  fi
}

# --- stage 5: download -----------------------------------------------------

ablit_sha256() { sha256sum "$1" | cut -d' ' -f1; }

# Download one file with range requests, then check size and hash before the rename.
# untested on a rented machine.
ablit_download_one() {
  local url="$1" rel="$2" want_bytes="$3" want_sha="$4"
  local dest="$ABLIT_DATA_DIR/$rel" part headers had attempt
  part="$dest.part"
  headers="$ABLIT_STATE_DIR/logs/last-download-headers.txt"
  mkdir -p "$(dirname "$dest")"

  if [ -f "$dest" ] && [ "$(ablit_file_size "$dest")" = "$want_bytes" ] && [ "$(ablit_sha256 "$dest")" = "$want_sha" ]; then
    return 0
  fi
  if [ "$(ablit_file_size "$part")" = "$want_bytes" ] && [ "$(ablit_sha256 "$part")" = "$want_sha" ]; then
    mv "$part" "$dest"
    return 0
  fi
  for attempt in 1 2 3 4 5; do
    had="$(ablit_file_size "$part")"
    if curl --silent --show-error --fail --location --continue-at - \
        --dump-header "$headers" --output "$part" "$url"; then
      break
    fi
    ablit_log "download of $rel failed (attempt $attempt)"
    [ "$attempt" -lt 5 ] || ablit_fail "download_failed" "$rel"
    sleep $((attempt * 5))
  done
  # A resumed request must be answered with 206. A 200 means the server ignored the range.
  if [ "$had" -gt 0 ] && [ "$had" -lt "$want_bytes" ] && ! grep -qE '^HTTP/[0-9.]+ 206' "$headers"; then
    rm -f "$part"
    ablit_fail "range_not_honored" "$rel"
  fi
  [ "$(ablit_file_size "$part")" = "$want_bytes" ] || ablit_fail "size_mismatch" "$rel"
  [ "$(ablit_sha256 "$part")" = "$want_sha" ] || { rm -f "$part"; ablit_fail "hash_mismatch" "$rel"; }
  mv "$part" "$dest"
}

ablit_download_all() {
  local url rel bytes sha _role
  while IFS=$'\t' read -r url rel bytes sha _role; do
    [ -n "$url" ] || continue
    ablit_log "downloading $rel ($bytes bytes)"
    ablit_download_one "$url" "$rel" "$bytes" "$sha"
  done <"$ABLIT_ARTIFACTS_TSV"
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
  local timeout="${ABLIT_READY_TIMEOUT_S:-900}" waited=0 code

  # 1. GET /v1/models with the key, until the engine answers or the timeout passes.
  while :; do
    code="$(_ablit_http_code -H "Authorization: Bearer ${ABLIT_ENDPOINT_KEY}" "$base/v1/models")"
    [ "$code" = "200" ] && break
    waited=$((waited + 5))
    [ "$waited" -lt "$timeout" ] || ablit_fail "health_timeout" "GET /v1/models returned $code after ${timeout}s"
    sleep 5
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
# only, log to $ABLIT_STATE_DIR/logs/engine.log, and remember the process ID.
# usage: ablit_start_engine KEY_VARIABLE_NAME command args...
ablit_start_engine() {
  local key_var="$1"
  shift
  mkdir -p "$ABLIT_STATE_DIR/logs"
  # The key goes into the child's environment through a builtin export in a
  # subshell, so it never appears in a command line that `ps` can show.
  (
    export "${key_var}=${ABLIT_ENDPOINT_KEY}"
    exec nohup "$@"
  ) >>"$ABLIT_STATE_DIR/logs/engine.log" 2>&1 &
  echo $! >"$ABLIT_STATE_DIR/engine.pid"
}
