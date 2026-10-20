#!/usr/bin/env bash
# Health check for the llama.cpp server. Run it on the rented machine.
#
# It checks that GET /v1/models answers with the key, that the server listens on
# 127.0.0.1 only, that one chat completion and one streamed completion work, and
# that requests with no key or a wrong key are refused.
# TODO(step 3): untested
set -euo pipefail

ENGINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
. "${ABLIT_LIB:-$ENGINE_DIR/../lib/common.sh}"
[ -f "${ABLIT_STATE_DIR}/env" ] && { set -a; . "${ABLIT_STATE_DIR}/env"; set +a; }

export ABLIT_READY_TIMEOUT_S="${ABLIT_READY_TIMEOUT_S:-600}"
ablit_health_check
echo "llamacpp health check passed"
