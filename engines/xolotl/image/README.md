# Xolotl container image

This folder builds the container image that ablit rents with when it serves a DeepSeek-V4.1 model through the Xolotl inference engine. The image holds the compiled engine, the Xolotl web server, the Python packages the server needs, and the license files. It holds no model weights. The bootstrap script downloads the weights onto the rented machine.

The Dockerfile is written from scratch for ablit. It uses the build commands that the Xolotl README documents, and it does not copy the Dockerfile that the Xolotl repository contains.

## Files

- `Dockerfile` builds the image in stages (`sources`, `build`, `base`, `app`, `pycheck`, `runtime`, `smoke`, `release`). The last stage, `release`, is the default.
- `requirements-runtime.txt` lists the Python packages that go into the image.
- `requirements-build.txt` lists the build tools (cmake and ninja). They stay out of the final image.
- `ds41-engine.sh` is installed as `/opt/xolotl/bin/ds41-engine`. It is the program that the server starts as its engine.
- `smoke.sh` is the smoke test that the workflow runs. It needs no GPU.
- `NOTICE` is the attribution text, installed at `/opt/xolotl/licenses/NOTICE`.
- `../../../.github/workflows/xolotl-image.yml` builds and publishes the image.

## What is in the image

Both base images are pinned by the digest of their multi-architecture index. The build uses the amd64 manifest.

| Role | Reference |
| --- | --- |
| Build base | `nvcr.io/nvidia/cuda:13.0.2-devel-ubuntu24.04@sha256:5dc1bca23d05bd37b011be68ec470c03b403a5da07ec3a86e41af9470e9d0cc6` |
| Runtime base | `nvcr.io/nvidia/cuda:13.0.2-runtime-ubuntu24.04@sha256:6a0e31b59e70890446f2c17356d6efc0d54260090a8c63d6ca7c2ad049db95d2` |

The same digests work on Docker Hub as `nvidia/cuda`. The Dockerfile uses nvcr.io because GitHub runners are limited when they pull anonymously from Docker Hub. NVIDIA can delete an old tag when a newer update release exists, and 13.0.3 exists today. If the 13.0.2 tag disappears, the fallback digests for 13.0.3 are `sha256:b7ae301dea2c162444795462ce17a05f6a516e5a75944b57af5b88540a1a2266` (devel) and `sha256:76f46f3e649824ebd3685d3d86c42b0ea62af04f59242f419f56cd5804a72efb` (runtime). Changing the base is a change to the image, so it needs a new tag and a new digest in `lock.yaml`.

CUDA 13.0.2 is the toolkit that the Xolotl author builds with. The author's setup script pins cuBLAS 13.0.2.14 and the CUDA runtime 13.0.96, and it warns that nvcc 13.2.0 and 13.2.1 produce wrong code for RTX 50 cards.

Sources, all fetched at build time and checked against the expected commit:

- Xolotl at `63fc34c6ea839d3e78e765b3e3aa72173116d352`, from `https://github.com/Indras-Mirror/Xolotl`.
- llama.cpp at `3cf03257f219afbe7334045ff7c6a06ac68c627d`. The Xolotl script `tools/xolotl/fetch-llama.sh` pins this commit and applies `tools/xolotl/ggml/xolotl-ggml.patch`. The Dockerfile stops if the script pins a different commit.

CMake flags: `-DSTRATA_ENABLE_CUDA=ON -DSTRATA_GGML_CUDA=ON -DSTRATA_MMQ_KQUANTS=ON -DSTRATA_DSV41_FQ_OP=ON -DSTRATA_DSV41_HC_MIX=ON`, as in the Xolotl README. Four more are set for a shared image.

- `-DSTRATA_PORTABLE=ON` compiles the CPU code for a baseline of AVX2. Without it, the code is tuned to the CPU of the build runner, and a rented host with another CPU could fail with an illegal instruction.
- `-DSTRATA_BUILD_TESTS=OFF` skips the test programs, as the Xolotl setup script does.
- `-DCMAKE_BUILD_TYPE=Release`.
- `-DCMAKE_CUDA_ARCHITECTURES=75;80;86;89;90;120a`.

Only the `ds41_generate` target is built. The build runs a second time if it fails the first time, because Xolotl documents that ptxas in CUDA 13.0 now and then fails on a file it has just written.

System packages in the runtime image: bash, coreutils, curl, ca-certificates, python3 (3.12), gawk and libstdc++6. The runtime base already has bash, flock and mawk. There is no sshd in the image, because Vast adds its own sshd when the instance uses the ssh runtype.

Python packages, installed from wheels and pinned to the versions in Xolotl's own `requirements.txt`:

| Package | Version | Use |
| --- | --- | --- |
| jinja2 | 3.1.6 | chat template |
| markupsafe | 3.0.3 | needed by jinja2 |
| regex | 2026.9.10 | tokenizer |
| psutil | 7.2.2 | server memory and process checks |

Pillow, numpy and jsonschema are left out. Without them the server cannot take image input, and it cannot validate JSON schemas for structured output (it prints a notice about jsonschema at start). Neither is needed for plain chat.

## Paths and how to run it

| What | Path |
| --- | --- |
| Engine binary | `/opt/xolotl/build-xolotl/ds41_generate` (also `/usr/local/bin/ds41_generate`) |
| Engine wrapper, used as `exe` | `/opt/xolotl/bin/ds41-engine` (also `/usr/local/bin/ds41-engine`) |
| Server entry point | `/opt/xolotl/tools/v41/serve/serve_ds41.py` |
| Engram extractor | `/opt/xolotl/tools/v41/extract_engram.py` |
| Sample configs | `/opt/xolotl/tools/v41/serve/strata-ds41.json` and `strata-ds41-1017.json` |
| DSpark expert counts | `/opt/xolotl/tools/v41/serve/dspark_expert_counts.txt` |
| Licenses | `/opt/xolotl/licenses/` |
| Build facts | `/opt/xolotl/BUILD-INFO` and `/opt/xolotl/ELF-ARCHS` |

The environment sets `XOLOTL_DIR=/opt/xolotl`, `PYTHONUNBUFFERED=1` and `STRATA_TEXT_LOG=0`. The working directory is `/opt/xolotl` and the default command is `bash`. A login over ssh may not receive the image environment, so the bootstrap should use absolute paths and export `STRATA_TEXT_LOG=0` itself.

The serve command is:

```
STRATA_API_KEY="$ABLIT_ENDPOINT_KEY" STRATA_TEXT_LOG=0 \
  python3 /opt/xolotl/tools/v41/serve/serve_ds41.py \
    --engine strata --config /ablit/xolotl-config.json --port "$ABLIT_PORT"
```

- The API key comes from the environment variable `STRATA_API_KEY`, and the server accepts it as `Authorization: Bearer <key>` or as `x-api-key`. The server exits with code 2 if the variable is empty. Never put the key on a command line.
- The server listens on `host` from the config, which defaults to `127.0.0.1`. That matches `bind: 127.0.0.1` in `lock.yaml`, so the endpoint is reached through an ssh tunnel.
- `STRATA_TEXT_LOG` defaults to writing a plain text copy of every answer under `/tmp`. Set it to `0`.
- Set `"api_monitor": false` in the config. When it is on, the server keeps the last 100 prompts and answers in memory and serves them at `/api-monitor`.

The config file is JSON, written by the bootstrap script. The keys that matter are listed here. The sample files above show every key.

- `exe` is `/opt/xolotl/bin/ds41-engine`. Do not use Xolotl's own `ds41-engine.sh`. It uses systemd-run and a lock file under the home directory, and a container has neither. The image does not include that script.
- `args` is the engine argument list. It holds `-m <gguf>`, `--backend cuda`, `--experts gpu`, `--engram <dir>[:<dir>]`, `--rank <rank file>`, `--ctx`, `--arena-gib`, `--kv-fp4`, `--route-bias` and `--prompt-batch`, with the values from the model card and the sample config.
- `cwd` is `/opt/xolotl`.
- `tokenizer` is a directory with `vocab.json`, `merges.txt`, `token_type.json` and `chat_template.jinja`.
- `model_name` is the name the endpoint reports, which ablit sets from `ABLIT_SERVED_NAME`.
- `port`, `gpu` and `host`.
- `env` may hold `STRATA_SERVE_DSPARK` (the path of the draft file, for example the Q2_K MTP file for the model) and `STRATA_ATTN_F16_GATHER`.
- `reasoning_budget_tokens`.

`ds41-engine` sets the defaults that the Xolotl documentation lists for this server and then starts `ds41_generate`. Each default can be changed in the `env` section of the config.

| Variable | Default in the wrapper |
| --- | --- |
| `STRATA_NO_LARGEPAGES` | `1` |
| `STRATA_SERVE_STEP_MAX` | `96` |
| `STRATA_SERVE_PASS_BIAS` | `0` |
| `STRATA_DSPARK_KEEP` | `<install>/tools/v41/serve/dspark_expert_counts.txt:64` |
| `STRATA_DSPARK_DEPTH` | `4` |
| `STRATA_DSPARK_CONF` | `0.6` |
| `STRATA_SERVE_LOOKUP` | `14` |
| `STRATA_SERVE_DSPARK_EVICT` | `4096` |
| `STRATA_DENSE_REQUANT` | `q6_K` |
| `STRATA_PRESTAGE_GIB` | `1.5` |
| `STRATA_PROMPT_BATCH` | unset. When set, it adds `--prompt-batch` after the configured arguments. |

`STRATA_SERVE_DSPARK` is unset by default, so speculative decoding stays off until the config names a draft file.

### Host requirements

- The NVIDIA driver on the host must be version 580 or newer. The runtime base sets `NVIDIA_REQUIRE_CUDA`, so the container runtime refuses to start on an older driver, except in the compatibility cases that NVIDIA lists. ablit should filter offers by driver or CUDA version before it rents.
- The GPU needs compute capability 7.5 or higher and must be one of the generations in the next table.
- The engine pins a memory arena of 60 GiB by default, as `min_memlock_kb` in `lock.yaml` records. The container needs a locked-memory limit at least that large, or the config needs a smaller `--arena-gib`.

## GPU architectures

CUDA 13 supports compute capability 7.5 and up, and it dropped 7.0 (V100). The catalog sets `compute_capability_min: 7.5`, so the image covers every generation CUDA 13 can compile for that ablit may rent, apart from the ones left out below. The compiled code is SASS (machine code for one generation), so a card needs an exact match for its generation. A newer generation does not run code built for an older one, except within the same family where the table says so.

| Value | GPUs | Why it is included |
| --- | --- | --- |
| 75 | RTX 20 series, T4 | The catalog floor is 7.5. Not tested by the Xolotl author. |
| 80 | A100 | Code for 86 does not run on 80, and the catalog floor admits A100. |
| 86 | RTX 30 series, A10, A40 | The author tested a 3090 on Windows. |
| 89 | RTX 40 series, L4, L40 | The author's main measurements were on an RTX 4090. |
| 90 | H100, H200 | Hopper. Not tested by the author. |
| 120a | RTX 50 series | Blackwell consumer cards. Plain 120 fails: ptxas rejects the FP4 block-scale instructions in the MXFP4 kernels unless the target is 120a. |

Left out: 70 (CUDA 13 cannot compile it), 100 and 103 (data center Blackwell), 87 (Jetson) and 121 (DGX Spark). Rented hosts with those GPUs fail the compute capability check. The build stops if the binary lacks code for a requested value, and `ELF-ARCHS` in the image lists what was found.

The Xolotl author measured only on RTX 4090 and RTX 3090 cards. Turing and Hopper are untested.

## Build and publish

The workflow `.github/workflows/xolotl-image.yml` runs only from the Actions tab (`workflow_dispatch`). It never runs by itself and never rents a machine.

1. It reads the Xolotl commit from `engines/xolotl/lock.yaml`, so the image and the lock cannot name different commits.
2. It frees disk space on the runner, then builds the `pycheck` stage. That stage runs the Python half of the smoke test within minutes, before the long compile starts.
3. It builds the `smoke` stage, which compiles the engine, and runs it. The smoke stage holds a stub of `libcuda` from the toolkit, because the runner has no GPU and the binary links the driver library. The test checks the tools, the file layout, the Python imports, the help text of the server and the Engram script, a mock server that must accept the right API key and answer 401 to a missing or wrong key, and `ds41_generate` (no missing libraries, usage text, exit code 2). It cannot load a model, so it does not show that the engine gives right answers.
4. It builds the `release` stage, which reuses the compiled layers, and tags it `ghcr.io/jameselkins/ablit-xolotl:<short commit>` and `...:<short commit>-<run number>`.
5. It logs in to ghcr.io with `GITHUB_TOKEN` (the job has `packages: write`), pushes both tags, reads the digest from the push output, and checks it against the manifest hash from the registry.
6. The job summary shows the digest and the three values to put in `lock.yaml`: `image`, `tag` and `digest`.

Inputs: `cuda_architectures` (default `75;80;86;89;90;120a`) and `push` (default true, set it to false for a trial build).

After the first push, open the package page on GitHub, link the package to the ablit repository, and make it public. Vast pulls the image without credentials, so a private package fails to start.

Compile time is the main risk. Six architectures on a four-core runner may take three to six hours, and a job stops after six hours. If a run times out, start it again with a shorter list, for example `86;89`, and then ablit must limit its offers to those generations. The Docker layer cache lives only on the runner, so a second run starts from scratch.

## Image size

The size has not been measured, because the image has not been built. The estimate is 4.7 to 5.1 GB on disk and about 2 GB to pull. That is the runtime base (4.19 GB on disk), about 50 MB of system packages, about 15 MB of Python packages, a small server tree, and a binary of roughly 300 to 700 MB with six architectures. The job summary prints the real size. The runtime base is large because it includes the shared cuBLAS libraries that the engine links.

A smaller base (`base` flavor, without cuBLAS) plus copied cuBLAS files would save about 2.5 GB. It was not chosen, because the NVIDIA container license describes a derived container as one that includes the entire NVIDIA container, and that was the clearer path for redistribution.

## Licenses

Verdict: no license in the image forbids redistribution. Publishing it publicly on ghcr.io is allowed as long as the notices stay in the image and the NVIDIA terms are passed on. The image keeps the notices in `/opt/xolotl/licenses/` and at `/NGC-DL-CONTAINER-LICENSE`.

| Component | License | Redistribution | What is required |
| --- | --- | --- | --- |
| Xolotl (Indras-Mirror/Xolotl) | MIT, copyright 2026 Niko1221 and the Strata contributors | Allowed | Keep the copyright and license text. Installed as `xolotl-LICENSE`. |
| Strata (the project Xolotl forks) | MIT | Allowed | Same notice. |
| llama.cpp and ggml | MIT, copyright 2023 to 2026 The ggml authors | Allowed | Keep the license text. Installed as `llama.cpp-LICENSE` and `ggml-LICENSE`. The Xolotl patch is shipped as `xolotl-ggml.patch`. |
| Outfit font (in `serve/web/fonts`) | SIL Open Font License 1.1 | Allowed with the program | `OFL.txt` stays next to the font files. |
| jinja2 3.1.6 | BSD-3-Clause | Allowed | Keep the license file inside the package. |
| markupsafe 3.0.3 | BSD-3-Clause | Allowed | Same. |
| psutil 7.2.2 | BSD-3-Clause | Allowed | Same. |
| regex 2026.9.10 | Apache-2.0 and CNRI-Python | Allowed | Keep the license files inside the package. |
| `dsv41_encoding.py` | No separate header. Part of the MIT repository and derived from DeepSeek's reference encoder. | Allowed | Covered by the Xolotl notice. |
| NVIDIA CUDA runtime and cuBLAS | CUDA Toolkit EULA, Attachment A lists cudart and cuBLAS as redistributable with an application | Allowed | See the next paragraph. |
| NVIDIA CUDA base images | NVIDIA Deep Learning Container License | Allowed for a compatible derived container | See the next paragraph. |
| Ubuntu packages (bash, coreutils, python3, curl, gawk and others) | GPL, LGPL and other open licenses | Allowed | Source is available from the Ubuntu archive. Copyright files stay in `/usr/share/doc`. |

The NVIDIA Deep Learning Container License (version of 14 September 2021) lets a person distribute a "Compatible" derived container, in section 1.c. The container must include the entire NVIDIA container plus other software with material added functionality. It must be distributed under terms at least as protective as NVIDIA's, it must not be sold as a stand-alone product, the NVIDIA software may be used only on NVIDIA GPU systems, and the distributor may not claim NVIDIA endorses it. The `NOTICE` file states these terms. Section 4.g forbids licensing the NVIDIA container itself under an open source license, so the image as a whole is not offered under MIT. The `org.opencontainers.image.licenses` label lists the components.

Xolotl's `data/` folder is under the Qwen Community License. It is not copied into the image.

The model weights are not in the image. Their licenses are covered by the catalog entries and the model cards, not by this folder.

## Does the pinned commit serve both catalog entries

The pin `63fc34c6ea839d3e78e765b3e3aa72173116d352` (dated 2026-10-20) should serve both `ds41-september-gsq-uncensored` and `ds41-1017-xq-v3-uncensored`, but nobody has booted it, so this is unverified until the first run on a rented machine.

- Both model cards were last updated on 2026-10-19, before the pin, and both use the same per-expert formats (PTQ1_0, Q2_0 and Q3_K) and the same build flags.
- The pin already contains `strata-ds41-1017.json`, which is the sample config for the 1017 build, and the DSpark sidecar support.
- The Xolotl branch is now 104 commits ahead of the pin (HEAD `b3452666`). The newer commits add batch slots, a conversation pool, multi-GPU layer split and sm_70 support. ablit needs none of them, so the pin stays.

Newer code is needed only if the first boot of one entry fails in a way the changelog explains. In that case, change the commit in `lock.yaml` and rebuild.

One finding the bootstrap author may want to test. At the pin, `Ds41Engram::open` already reads `model.safetensors.index.json` and its shards from the directories named in `--engram`. If so, `extract_engram.py` (which rewrites about 203 GB) can be skipped by pointing `--engram` at a directory that holds the two FP8 shards and the index file. This is untested, and the catalog does not list `model.safetensors.index.json` or `config.json` as files to download. If it fails, run `extract_engram.py SRC_DIR --to layers.1=DIR_A --to layers.14=DIR_B`, which takes the source directory and one `--to` per layer group. With no arguments it prints its usage and exits with code 2.

## Known limits

- The apt packages are not pinned to versions, so two builds of the same commit can differ in system packages. The base images and the Python versions are pinned. The Python packages are pinned by version only, with no hashes, because the package index could not be read while this folder was written.
- The image has not been built. `docker build --check` reports no warnings, the shell scripts pass shellcheck, and the source fetch, file assembly and Python smoke test were run against simulated trees. The compile and the binary smoke test run for the first time in the workflow.
- `pyproject.toml` force-includes the `engines` folder in the wheel, so these files, including this README and the Dockerfile, ship in the PyPI package. They add about 50 KB.
- The mock server in the smoke test uses the generic Xolotl server. The DeepSeek wrapper (`serve_ds41.py`) needs the real tokenizer files, so the test covers it only through `--help`.
