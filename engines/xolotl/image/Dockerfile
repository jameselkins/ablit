# Container image for the Xolotl inference engine, used by ablit.
#
# This file was written for ablit from the build steps in the Xolotl README. It
# is not a copy of any Dockerfile from Xolotl or another project.
#
# The image holds the DeepSeek-V4.1 engine (ds41_generate), the OpenAI and
# Anthropic compatible server written in Python, and the Engram extraction
# script. It holds no model weights and no sshd, because Vast adds sshd itself.
#
# Build it from this directory:
#   docker build --target release -t ablit-xolotl .
# The build compiles CUDA code for six GPU generations. Expect several hours on
# a small machine, so the GitHub workflow in .github/workflows/xolotl-image.yml
# is the normal way to build it.
#
# Stages:
#   sources  fetches Xolotl and llama.cpp at pinned commits and installs the
#            pinned Python packages. It needs no compiler.
#   build    compiles ds41_generate.
#   base     the NVIDIA runtime image plus the few system packages the engine
#            scripts use.
#   app      base plus the server, the Python packages and the licenses.
#   pycheck  app plus the Python-only part of the smoke test. It runs during
#            the build and needs no binary, so a mistake shows up in minutes
#            instead of after the compile.
#   runtime  app plus the compiled binary.
#   smoke    runtime plus a stub of the GPU driver library, so the binary can
#            be started on a machine with no GPU. Used only by the workflow.
#   release  runtime plus labels. This is the last stage, so it is the default.

# Both CUDA images are pinned by the digest of their multi-architecture index.
# CUDA 13.0.2 is the toolkit that the Xolotl authors build and measure with.
# Their setup script pins cuBLAS 13.0.2.14 and the CUDA runtime 13.0.96, and it
# warns that nvcc 13.2.0 and 13.2.1 compile wrong code for RTX 50 cards.
ARG CUDA_DEVEL_IMAGE=nvcr.io/nvidia/cuda:13.0.2-devel-ubuntu24.04@sha256:5dc1bca23d05bd37b011be68ec470c03b403a5da07ec3a86e41af9470e9d0cc6
ARG CUDA_RUNTIME_IMAGE=nvcr.io/nvidia/cuda:13.0.2-runtime-ubuntu24.04@sha256:6a0e31b59e70890446f2c17356d6efc0d54260090a8c63d6ca7c2ad049db95d2

# The Xolotl commit that ablit pins in engines/xolotl/lock.yaml.
ARG XOLOTL_COMMIT=63fc34c6ea839d3e78e765b3e3aa72173116d352
# One entry per GPU generation: Turing, A100, RTX 30 and A-series, RTX 40 and L40,
# H100, and RTX 50. See the README beside this file.
ARG CUDA_ARCHITECTURES="75;80;86;89;90;120a"

# ---------------------------------------------------------------------------
# sources: source code and Python packages
# ---------------------------------------------------------------------------
FROM ${CUDA_DEVEL_IMAGE} AS sources

ARG XOLOTL_COMMIT
ARG CUDA_ARCHITECTURES
ARG CUDA_DEVEL_IMAGE
ARG CUDA_RUNTIME_IMAGE
# The llama.cpp commit that tools/xolotl/fetch-llama.sh pins at that Xolotl
# commit. The build stops if the script names a different commit, so a change in
# Xolotl cannot slip in unnoticed.
ARG LLAMA_CPP_COMMIT=3cf03257f219afbe7334045ff7c6a06ac68c627d

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*

# Fetch only the pinned commit, and stop if the commit that arrives is another one.
WORKDIR /src/xolotl
RUN git init --quiet . \
 && git remote add origin https://github.com/Indras-Mirror/Xolotl.git \
 && git fetch --quiet --depth 1 origin "${XOLOTL_COMMIT}" \
 && git checkout --quiet FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = "${XOLOTL_COMMIT}"

# Run Xolotl's own fetch-llama.sh. It checks out llama.cpp at its pinned commit
# and applies the Xolotl ggml patch. The folder is prepared with a shallow fetch
# first, so the script does not clone the whole llama.cpp history.
RUN set -eu; \
    pin="$(sed -n 's/^PIN=\([0-9a-f]\{40\}\)$/\1/p' tools/xolotl/fetch-llama.sh)"; \
    if [ "$pin" != "${LLAMA_CPP_COMMIT}" ]; then \
      echo "fetch-llama.sh pins llama.cpp ${pin} but this Dockerfile expects ${LLAMA_CPP_COMMIT}" >&2; \
      exit 1; \
    fi; \
    mkdir -p third_party/llama.cpp; \
    git -C third_party/llama.cpp init --quiet; \
    git -C third_party/llama.cpp remote add origin https://github.com/ggml-org/llama.cpp.git; \
    git -C third_party/llama.cpp fetch --quiet --depth 1 origin "$pin"; \
    bash tools/xolotl/fetch-llama.sh; \
    test "$(git -C third_party/llama.cpp rev-parse HEAD)" = "$pin"

# Python packages for the server, pinned to the versions in Xolotl's own
# requirements.txt. The build tools get their own environment, so they stay out
# of the final image. Packages come from wheels only, so nothing compiles here.
COPY requirements-runtime.txt requirements-build.txt /tmp/
RUN python3 -m venv /opt/buildenv \
 && /opt/buildenv/bin/pip install --no-cache-dir --only-binary=:all: --no-deps \
      --target /opt/pysite -r /tmp/requirements-runtime.txt

# Collect everything that goes into the final image under /out. The server
# needs the serve folder and a few modules from tools, in the same layout as the
# Xolotl repository, because the server finds them by relative path.
COPY ds41-engine.sh smoke.sh NOTICE /tmp/context/
RUN set -eu; \
    app=/out/opt/xolotl; \
    install -d "$app/bin" "$app/tools/v41" "$app/licenses" "$app/smoke"; \
    cp -a serve "$app/serve"; \
    rm -rf "$app/serve/fixtures" "$app/serve/chat_golden.json" "$app/serve/mcp_fake_server.py"; \
    find "$app/serve" -type d -name __pycache__ -prune -exec rm -rf {} +; \
    find "$app/serve" -maxdepth 1 -type f -name 'test_*.py' -delete; \
    cp tools/strata_tokenizer.py tools/gguf_reader.py tools/_paths.py "$app/tools/"; \
    cp tools/v41/dsv41_encoding.py tools/v41/extract_engram.py "$app/tools/v41/"; \
    cp -a tools/v41/serve "$app/tools/v41/serve"; \
    rm -f "$app/tools/v41/serve/test_serve_ds41.py" "$app/tools/v41/serve/ds41-engine.sh" \
          "$app/tools/v41/serve/strata-ds41-quetza.wrapper" "$app/tools/v41/serve/export_tokenizer.py" \
          "$app/tools/v41/serve/drive_engine.py" "$app/tools/v41/serve"/*-win.json; \
    install -m 0755 /tmp/context/ds41-engine.sh "$app/bin/ds41-engine"; \
    install -m 0755 /tmp/context/smoke.sh "$app/smoke/smoke.sh"; \
    install -m 0644 /tmp/context/NOTICE "$app/licenses/NOTICE"; \
    install -m 0644 LICENSE "$app/licenses/xolotl-LICENSE"; \
    install -m 0644 third_party/ggml/LICENSE "$app/licenses/ggml-LICENSE"; \
    install -m 0644 third_party/llama.cpp/LICENSE "$app/licenses/llama.cpp-LICENSE"; \
    install -m 0644 tools/xolotl/ggml/xolotl-ggml.patch "$app/licenses/xolotl-ggml.patch"; \
    { \
      echo "xolotl_repo=https://github.com/Indras-Mirror/Xolotl"; \
      echo "xolotl_commit=${XOLOTL_COMMIT}"; \
      echo "llama_cpp_commit=${LLAMA_CPP_COMMIT}"; \
      echo "cuda_architectures=$(printf '%s' "${CUDA_ARCHITECTURES}" | tr ';' ',')"; \
      echo "cuda_devel_image=${CUDA_DEVEL_IMAGE}"; \
      echo "cuda_runtime_image=${CUDA_RUNTIME_IMAGE}"; \
    } > "$app/BUILD-INFO"; \
    mkdir -p /out/usr/local/lib/python3.12; \
    cp -a /opt/pysite /out/usr/local/lib/python3.12/dist-packages; \
    find /out/usr/local/lib/python3.12/dist-packages -type d -name __pycache__ -prune -exec rm -rf {} +

# ---------------------------------------------------------------------------
# build: compile the engine
# ---------------------------------------------------------------------------
FROM sources AS build

ARG CUDA_ARCHITECTURES
# Each compile job can use a few gigabytes of memory. Four jobs fit the 16 GB
# runners that GitHub provides.
ARG BUILD_JOBS=4

RUN apt-get update \
 && apt-get install -y --no-install-recommends g++ \
 && rm -rf /var/lib/apt/lists/*
RUN /opt/buildenv/bin/pip install --no-cache-dir --only-binary=:all: -r /tmp/requirements-build.txt
# cmake and ninja come from the pinned Python packages, not from apt.
ENV PATH="/opt/buildenv/bin:${PATH}"

WORKDIR /src/xolotl
# The flags come from the Xolotl README (CUDA, ggml CUDA, K-quant kernels, and
# the two fused DeepSeek-V4.1 operations). Three more are set for a shared image:
#   STRATA_PORTABLE=ON   the CPU code uses an AVX2 baseline instead of the
#                        features of the build machine, so it runs on any
#                        rented host with AVX2.
#   STRATA_BUILD_TESTS=OFF  skips the test programs, as Xolotl's setup does.
#   CMAKE_CUDA_ARCHITECTURES  one entry per GPU generation. See the README
#                        beside this file for what each number covers.
RUN cmake -S . -B build-xolotl -G Ninja \
      -DCMAKE_BUILD_TYPE=Release \
      -DSTRATA_ENABLE_CUDA=ON \
      -DSTRATA_GGML_CUDA=ON \
      -DSTRATA_MMQ_KQUANTS=ON \
      -DSTRATA_DSV41_FQ_OP=ON \
      -DSTRATA_DSV41_HC_MIX=ON \
      -DSTRATA_GGML_DIR=/src/xolotl/third_party/llama.cpp \
      -DSTRATA_PORTABLE=ON \
      -DSTRATA_BUILD_TESTS=OFF \
      "-DCMAKE_CUDA_ARCHITECTURES=${CUDA_ARCHITECTURES}"

# Xolotl's own setup script runs a failed build a second time, because CUDA 13.0
# ptxas now and then fails to read a file it has just written. This does the same.
RUN set -eu; \
    build() { cmake --build build-xolotl --target ds41_generate -j "${BUILD_JOBS}"; }; \
    build || { echo "The build stopped. Running it once more."; build; }; \
    test -x build-xolotl/ds41_generate; \
    ls -l build-xolotl/ds41_generate; \
    (ldd build-xolotl/ds41_generate || true)

# List the GPU code inside the binary and stop if a requested generation is
# missing. ggml renames 120 to 120a, so the check compares the number only. If
# cuobjdump prints nothing, the check says so and does not fail the build.
RUN set -eu; \
    cuobjdump --list-elf build-xolotl/ds41_generate | grep -o 'sm_[0-9]*' | sort -u > build-xolotl/ELF-ARCHS || true; \
    echo "GPU code found in ds41_generate:"; cat build-xolotl/ELF-ARCHS; \
    if [ -s build-xolotl/ELF-ARCHS ]; then \
      for arch in $(printf '%s' "${CUDA_ARCHITECTURES}" | tr ';' ' '); do \
        grep -q "^sm_${arch%[af]}$" build-xolotl/ELF-ARCHS || { echo "No GPU code for sm_${arch} in the binary." >&2; exit 1; }; \
      done; \
    else \
      echo "cuobjdump listed nothing, so the architecture check was skipped."; \
    fi

# ---------------------------------------------------------------------------
# base: runtime image and system packages
# ---------------------------------------------------------------------------
FROM ${CUDA_RUNTIME_IMAGE} AS base

# bash, curl, python3, coreutils and ca-certificates are what the ablit engine
# scripts and the server use. gawk is here because the scripts do arithmetic on
# byte counts that exceed 32 bits. libstdc++6 is listed so the engine never
# depends on another package pulling it in.
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends bash coreutils curl ca-certificates python3 gawk libstdc++6 \
 && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# app: the server, the Python packages and the licenses
# ---------------------------------------------------------------------------
FROM base AS app

COPY --from=sources /out/ /
RUN python3 -m compileall -q /opt/xolotl /usr/local/lib/python3.12/dist-packages \
 && ln -s /opt/xolotl/bin/ds41-engine /usr/local/bin/ds41-engine

# ---------------------------------------------------------------------------
# pycheck: the part of the smoke test that needs no binary
# ---------------------------------------------------------------------------
FROM app AS pycheck
RUN bash /opt/xolotl/smoke/smoke.sh python

# ---------------------------------------------------------------------------
# runtime: app plus the compiled engine
# ---------------------------------------------------------------------------
FROM app AS runtime

COPY --from=build /src/xolotl/build-xolotl/ds41_generate /opt/xolotl/build-xolotl/ds41_generate
COPY --from=build /src/xolotl/build-xolotl/ELF-ARCHS /opt/xolotl/ELF-ARCHS
RUN ln -s /opt/xolotl/build-xolotl/ds41_generate /usr/local/bin/ds41_generate

# The ablit scripts read these through the environment. A login over ssh may
# not pass them on, so each script also falls back to the same paths.
ENV XOLOTL_DIR=/opt/xolotl \
    PYTHONUNBUFFERED=1 \
    STRATA_TEXT_LOG=0
WORKDIR /opt/xolotl
CMD ["bash"]

# ---------------------------------------------------------------------------
# smoke: runtime plus a stub of libcuda, for machines with no GPU
# ---------------------------------------------------------------------------
FROM runtime AS smoke

# The engine links the GPU driver library, which exists only on a machine with
# a driver. The stub in the CUDA toolkit has the same names, so the binary can
# start far enough to print its usage text. The stub lives outside the library
# path, and only the smoke test puts it there.
COPY --from=build /usr/local/cuda/lib64/stubs/libcuda.so /opt/xolotl/smoke/stubs/libcuda.so.1
ENV LD_LIBRARY_PATH=/opt/xolotl/smoke/stubs:/usr/local/nvidia/lib:/usr/local/nvidia/lib64:/usr/local/cuda/lib64
CMD ["bash", "/opt/xolotl/smoke/smoke.sh", "all"]

# ---------------------------------------------------------------------------
# release: the image that gets published
# ---------------------------------------------------------------------------
FROM runtime AS release

ARG XOLOTL_COMMIT
ARG CUDA_ARCHITECTURES
LABEL org.opencontainers.image.title="ablit-xolotl" \
      org.opencontainers.image.description="Xolotl inference engine for DeepSeek-V4.1 models, built for ablit. CUDA 13. No model weights." \
      org.opencontainers.image.source="https://github.com/jameselkins/ablit" \
      org.opencontainers.image.licenses="MIT AND OFL-1.1 AND BSD-3-Clause AND Apache-2.0 AND LicenseRef-NVIDIA-Deep-Learning-Container-License" \
      io.github.jameselkins.ablit.xolotl-commit="${XOLOTL_COMMIT}" \
      io.github.jameselkins.ablit.cuda-architectures="${CUDA_ARCHITECTURES}"
