#!/usr/bin/env bash
# Starts the DeepSeek-V4.1 engine for the Xolotl server inside the ablit image.
#
# The Xolotl server runs the file named by "exe" in its config and adds the
# engine arguments. This script is that file. It sets the environment defaults
# that Xolotl documents for the DeepSeek-V4.1 server, then replaces itself with
# ds41_generate, so the server talks to the engine directly.
#
# Xolotl's own wrapper script starts the engine inside a systemd scope, and it
# takes a lock file in the home directory. A rented container has neither
# systemd nor a home directory worth locking, so this script does neither. The
# container's own memory limit takes the place of the limit that script sets.
#
# Every default below can be changed by setting the variable in the "env"
# section of the server config. A variable that is set but empty turns the
# feature off where the comment says so.
set -euo pipefail

XOLOTL_DIR="${XOLOTL_DIR:-/opt/xolotl}"
ENGINE="$XOLOTL_DIR/build-xolotl/ds41_generate"
[ -x "$ENGINE" ] || { echo "ds41-engine: $ENGINE is missing or not executable" >&2; exit 127; }

export GGML_NO_BACKTRACE=1

# Pinned memory is mapped in 4 KiB pages. Xolotl measured that this loads the
# 60 GiB arena in about 50 seconds, against about 4 minutes with huge pages.
export STRATA_NO_LARGEPAGES="${STRATA_NO_LARGEPAGES:-1}"

# A turn of up to 96 new tokens runs as exact passes with no routing bias. A
# longer turn runs as one exact batch.
export STRATA_SERVE_STEP_MAX="${STRATA_SERVE_STEP_MAX:-96}"
export STRATA_SERVE_PASS_BIAS="${STRATA_SERVE_PASS_BIAS:-0}"

# DSpark speculative decoding. STRATA_SERVE_DSPARK must name the draft file,
# for example V41-MTP-Vision.Q2_K.gguf for the September build. It is off when
# the variable is unset or empty. The settings below apply only when it is on.
if [ -z "${STRATA_SERVE_DSPARK:-}" ]; then
  unset STRATA_SERVE_DSPARK
fi
export STRATA_DSPARK_KEEP="${STRATA_DSPARK_KEEP:-$XOLOTL_DIR/tools/v41/serve/dspark_expert_counts.txt:64}"
export STRATA_DSPARK_DEPTH="${STRATA_DSPARK_DEPTH:-4}"
export STRATA_DSPARK_CONF="${STRATA_DSPARK_CONF:-0.6}"

# Drafts copied from the prompt (up to 14 tokens) for edits and rewrites, and
# the draft weights leave the card during a prompt batch of 4096 new tokens or
# more. Set STRATA_SERVE_LOOKUP=0 to turn the first one off.
export STRATA_SERVE_LOOKUP="${STRATA_SERVE_LOOKUP:-14}"
export STRATA_SERVE_DSPARK_EVICT="${STRATA_SERVE_DSPARK_EVICT:-4096}"

# The dense half of the model is stored as Q8_0 and is converted to Q6_K when
# it loads. That frees about 1.7 GiB of VRAM for experts. Setting the variable
# to an empty value turns the conversion off.
export STRATA_DENSE_REQUANT="${STRATA_DENSE_REQUANT-q6_K}"

# Size of the region that stages a prompt batch. Xolotl measured 1.5 GiB with a
# 12288 token batch on a 24 GB card. Zero turns it off.
export STRATA_PRESTAGE_GIB="${STRATA_PRESTAGE_GIB:-1.5}"

# A value in STRATA_PROMPT_BATCH wins over the --prompt-batch in the config,
# because it is added after the configured arguments.
if [ -n "${STRATA_PROMPT_BATCH:-}" ]; then
  set -- "$@" --prompt-batch "$STRATA_PROMPT_BATCH"
fi

exec "$ENGINE" "$@"
