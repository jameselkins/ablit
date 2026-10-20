# ablit build specification

This file tells an implementer what to build. It covers the command-line interface, the catalog, the planner, the drivers, the engine templates, the referral rules and the order of work. Nothing described here has run against a paid machine yet. Every hardware floor in the seed catalog stays marked `unverified` until a boot test records a result.

## What ablit does

ablit is a command-line tool that runs on the user's machine with the user's own marketplace accounts. Given a model, it finds the cheapest rented GPU machine that meets the model's needs, shows the full cost, and asks the user to confirm. It then rents the machine, starts the inference engine, downloads the weights, checks that the engine answers, and prints an OpenAI-compatible endpoint.

ablit runs no service, holds no money and relays no inference traffic. It never chooses a machine because of a referral commission, and the tests in "Rule against steering by commission" enforce that rule.

## Commands

| Command | Behavior |
|---|---|
| `ablit setup [backend]` | First-run account flow. Keys go to the OS keyring, or are kept for the session only. ablit never writes a plaintext key without asking. |
| `ablit plan <model>` | Resolves the model to one catalog entry, filters and ranks offers from every enabled backend, and prints the chosen offer, the next four, and why other backends or offers were dropped. Rents nothing. |
| `ablit up <model>` | Runs `plan`, shows the quote, and asks the user to type `rent`. It then re-queries the offer, writes a local intent record, rents, starts the watchdog, bootstraps the engine, checks health, and prints the endpoint. |
| `ablit status [run]` | Run state, resource IDs, time of the last marketplace check, spend against the cap, bootstrap stage, and pending cleanup. Supports `--all` and `--json`. |
| `ablit logs [run] [--stage bootstrap\|engine\|watchdog] [-f]` | Streams logs from the box over SSH, with secrets removed. |
| `ablit tunnel [run]` | Re-opens the SSH tunnel to a running endpoint. |
| `ablit down [run]` | Destroys every billable resource the run created and confirms with the marketplace. Never asks for confirmation. Works even for a backend whose new launches are disabled. |
| `ablit catalog list\|show <id>\|validate <file>` | Lists entries and their test status, explains one entry, or checks a contributor's file. |

`<model>` is a catalog ID or a Hugging Face repo ID. A repo ID that matches several entries, e.g. two quant versions, requires `--variant`. ablit never replaces one build with another. A repo that isn't in the catalog is refused with exit code 3 and a link to CONTRIBUTING.

### Flags for `plan` and `up`

| Flag | Default | Meaning |
|---|---|---|
| `--max-spend USD` | Required for `up`, with no default. May come from the config file, and the quote shows the resolved value. | The spend at which ablit destroys the run. It counts compute, disk, the model download and transfer charges. |
| `--hours N` | 1 | Session length used to rank offers. It doesn't stop the run. |
| `--spot` | off | Allows interruptible offers, after a warning. Disabled until a spot round trip has been tested. |
| `--min-download MBPS` | the entry's value, or 500 | Floor on download speed |
| `--trust any\|verified` | `any` | `verified` keeps only Vast verified hosts and RunPod Secure Cloud. `any` shows the status without filtering. |
| `--min-reliability`, `--region`, `--max-price-hr` | none | Optional user filters |
| `--backends vast,runpod` | all enabled backends with keys | Limits the search. Output lists the backends left out and why. |
| `--variant ID` | none | Picks a build when a repo has several |
| `--endpoint tunnel\|public` | `tunnel` | See "Endpoint" |
| `--untested` | off | Required to run `up` on an `unverified` entry |
| `--catalog PATH` | bundled catalog | Local catalog for testing. Paid launch from it also needs `--untested`. |
| `--json` | off | Versioned machine-readable output |

There is no `--yes`. A paid `up` without a terminal exits with code 2.

### Quote

The quote shows these items:

1. Offer ID, marketplace, GPU model and count, VRAM, host RAM, allocated disk, advertised download speed, verification status and remaining rental duration.
2. Model build and pinned revision, engine and pinned image digest, and the entry's test status.
3. All-in hourly price, the GPU-only sticker price in a separate column, the per-GB transfer price, and one-time costs.
4. Totals for 1 hour, 24 hours, 7 days and `--hours`, each including download charges and the hours spent downloading and starting.
5. The cap, the time at which spend would reach it, and the spend-cap notice.

The spend-cap notice is printed on every quote, in the README and in `ablit up --help`:

```
How the spend cap works: a watchdog on the rented machine destroys it when
spend reaches $<cap>, and ablit checks too while it is running on this computer.
If the rented machine freezes or loses its network while this computer is off
or asleep, neither check can run, and the marketplace keeps billing until you
run `ablit down` or the rental ends. Check `ablit status` if you leave a run
unattended.
```

When a marketplace deadline has passed its own test, the notice adds "<Marketplace> will also end this rental at <time>, even if both checks fail." Until then the notice doesn't mention it.

The spot warning:

```
An interruptible machine can be taken away at any time. Your endpoint will stop
without notice, downloaded files may be lost, and on Vast a paused machine's
disk is still billed. The download and setup charges count toward your cap.
```

Any change to the price, offer, build or cap cancels the confirmation. If the offer is gone at re-query, ablit plans again and asks again.

### Run lifecycle

1. Before creating anything, write `runs/<run-id>.json` with the approved quote and state `creating`. Write through a temporary file and rename, and flush to disk.
2. Put the run ID in the instance label. If a create request times out or the response is lost, list the user's instances and look for the label before doing anything else. Never send a second create for the same run.
3. Record every resource ID, including volumes, as soon as it is known.
4. Use a lock file per run. Refuse a second active run of the same model unless the user passes `--another`.
5. `down` moves through `termination_requested`, `resources_terminated` and `billing_stopped_confirmed`. Print "Billing stopped" only in the last state. Otherwise keep the run file, print what is still running and the marketplace page to check, and exit with code 7. Running `down` again retries.
6. Print the final cost only when the marketplace reports it.

State lives in the platform's user state directory. There is no database and no daemon.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 2 | Usage error, including a paid `up` without a terminal |
| 3 | Model not supported, or no offer qualified |
| 4 | Missing or rejected credentials |
| 5 | Quote incomplete, or the cap can't cover setup and 10 minutes of use |
| 6 | Bootstrap, speed check or health check failed |
| 7 | Cleanup pending, or billing stop not confirmed |
| 8 | Cap reached, and the watchdog destroyed the run |
| 9 | The marketplace rejected the workload. ablit cleans up and doesn't retry elsewhere. |

## Catalog

### Format

Each build is one YAML file at `catalog/models/<id>.yaml`, checked by `catalog/schema/v1.json`. CI compiles all entries into `catalog/dist/catalog.json` and records its SHA-256. Each CLI release pins the digest of its catalog, and every quote records it. Catalog releases use the version form `YYYY.MM.DD.N`. The integer `schema` field changes only when the format changes, and a CLI release refuses schema versions it doesn't know.

Catalog data is CC0-1.0. Each entry records its model's own license, which the catalog license doesn't change. Entries hold data only, and all commands live in the reviewed engine templates.

```yaml
schema: 1
id: ds41-1017-xq-v3-uncensored
display_name: DeepSeek-V4.1-Flash 1017, XQ V3 uncensored quant (Xolotl)
source:
  repo: IndrasMirror/DeepSeek-V4.1-Flash-1017-XQ-Uncensored
  revision: 9d3c01d86709261e51d566b02e1dda75f40d009a
  license: mit
  base_model: deepseek-ai/DeepSeek-V4.1-Flash-1017
  modification: abliterated        # none | abliterated | finetune | quantized
artifacts:                         # every file the profile reads
  - {repo: ..., revision: ..., path: ..., bytes: ..., sha256: ..., role: weight}
  - {path: ..., bytes: ..., git_blob_id: ..., role: tokenizer}   # small files with no LFS hash
staging:                           # order of steps, used to compute peak disk
  - {step: download, artifacts: all}
  - {step: join, inputs: [...], output_bytes: ...}
  - {step: engram-extract, inputs: [...], output_bytes: ..., keeps_inputs_until_done: true}
profile:
  engine: xolotl
  engine_lock: xolotl-63fc34c
  gpu: {vendor: nvidia, count: 1, vram_gb_each: 24, compute_capability_min: 7.5}
  host_ram_gb: 90
  host_ram_gb_recommended: 256
  storage: {random_read_required: true}
  context_tokens: 4096
  concurrency: 1
  port: 8150
  startup_hours_estimate: 0.5
  min_download_mbps: 500
sources: [url, ...]
tests: []
status: unverified                 # unverified | boot-tested | removed
```

Rules for the schema:

1. `artifacts` lists every file the engine reads, including draft models and tokenizer files.
2. Large files carry the Hugging Face LFS SHA-256. Small files without an LFS record carry the Git blob ID, and `verify_entry.py` downloads them at the pinned revision to compute a SHA-256.
3. Peak disk is computed from `staging`, never written by hand. Until a streaming Engram extractor is proven, the Xolotl staging keeps both Engram source shards on disk during extraction.
4. `profile` is one exact combination of engine lock, GPU arrangement, context length and concurrency. A test of one profile doesn't approve another.

### Status and launch rules

An entry is `unverified` once CI passes and a maintainer merges it. It becomes `boot-tested` when a `tests` record holds a log from a real run of that profile. `ablit up --report` writes that log locally with secrets removed, and the user decides whether to share it.

Launching an `unverified` entry needs `--untested`, and the quote then says "Nobody has booted this entry yet. The engine may fail to start, and you pay for the time until ablit destroys the run." A `removed` entry can't be launched. Before renting, ablit checks that every artifact is still public at its pinned revision.

### Contributions

Anyone can open a pull request with an entry. CI runs these checks and never rents anything:

1. Schema validation.
2. `tools/verify_entry.py` confirms each repo and revision, and that each artifact's size and hash match the Hugging Face API with `?blobs=true`. The tree API is paged at 50 entries, so it isn't used.
3. Every number in `profile` has a URL in `sources`.
4. The computed staging peak matches what the entry claims.
5. For llama.cpp entries, the GGUF `general.architecture` is supported by the pinned llama.cpp build.

A weekly job re-runs check 2. When an artifact has disappeared, it opens a pull request that sets the entry to `removed`. The pre-rent check already refuses such entries in installed copies.

### Seed entries

| ID | Engine | Floors | Notes |
|---|---|---|---|
| `ds41-1017-abl-fp8` | vLLM | 8 GPUs, 614 GB VRAM in total, 256 GB host RAM, disk from staging | Floors derived from the vLLM recipe, not yet tested |
| `ds41-september-gsq-uncensored` | Xolotl | One 24 GB NVIDIA GPU, 90 GB host RAM (256 GB recommended), disk from staging | Floors from the Xolotl README, not yet tested |
| `ds41-1017-xq-v2-uncensored` | Xolotl | Same | Separate entry from V3 |
| `ds41-1017-xq-v3-uncensored` | Xolotl | Same | |
| `ds41-1017-gsq-agentic-strata` | Xolotl | Same | Unmodified 1017 base |
| `llama33-70b-fp8-dynamic` | vLLM | From its sources | 70B-class vLLM build |
| `llama31-8b-abliterated-q4km` | llama.cpp | From its sources | First paid test target |

## Planner

### Filter chain, in order

The planner records how many offers each step removed.

1. Engine floor: GPU vendor, count, VRAM per GPU and in total, compute capability, host RAM and OS. The quote shows the GPU model.
2. Disk floor: free disk at least the staging peak plus the engine image. On Vast, send `disk_space >= N` and `allocated_storage = N` so the price includes the disk.
3. Download speed floor: Vast's per-host `inet_down` must meet the floor. RunPod reports no per-host speed, so its offers are kept, labeled "speed not advertised, measured after boot", and ranked as if they ran exactly at the floor.
4. Rental type and availability: on demand unless `--spot`, Vast `rentable = true`, RunPod `stockStatus` not null, and remaining rental duration at least the startup estimate plus `--hours`.
5. User filters, only when set.
6. Rank by total cost with the search rule below.

### Total cost

`download_gb` is in decimal GB, and speeds are in megabits per second.

```
speed_mbps = advertised download speed, or the floor when none is advertised
D          = download_gb * 8000 / (speed_mbps * 3600)
S          = profile.startup_hours_estimate
total(H)   = hourly_all_in * (D + S + H)
           + bandwidth_cost_per_gb * (download_gb + image_gb)
           + fixed_fees
```

`hourly_all_in` is Vast `dph_total` at the needed `allocated_storage`, or RunPod `uninterruptablePrice` for all GPUs plus volume disk. `bandwidth_cost_per_gb` is Vast's `inet_down_cost`, and zero for RunPod. The quote shows `D` and `S` on separate lines and labels `S` an estimate.

### Search rule

Vast returns at most 64 rows per query, so the first 64 rows by hourly price can miss the lowest total. Use this rule:

1. Query with all filters on the server, ordered by `dph_total` ascending.
2. Compute `total(H)` for each row, and keep the lowest.
3. No unseen offer can cost less than `last_dph * (H + S)`, where `last_dph` is the last row's `dph_total`.
4. If the lowest total is at or below that bound, stop.
5. Otherwise query again with `dph_total > last_dph`. If Vast rejects that filter, or after 10 pages, stop and label the result "cheapest of the N offers checked".

If a backend fails or times out, the output names it and says the comparison didn't include it.

### Rule against steering by commission

The ranked list is a function only of the offers, the catalog profile and the user's flags. Referral data isn't an input to search, filtering or ranking. CI enforces it with these tests:

1. `ablit/select/` and `ablit/backends/` can't import `ablit/referral/`, enforced by an import-linter contract.
2. The planner gives the same filtered set, order and selection with referral IDs set for all marketplaces, for none, and with the paying marketplaces shuffled.
3. A `hypothesis` property test generates offer sets, including sets larger than 64 with a cheap-transfer offer outside the first 64 by hourly price. The first-ranked offer must have the lowest total among all offers that pass the filters.
4. A cheaper backend without a referral program beats a more expensive backend with one.
5. Ties break on backend name, then offer ID, alphabetically.

`POLICY.md` states the rule and names these tests.

### Spend cap

1. Watchdog on the box. The bootstrap starts `ablit-watchdog`, a single Python file with no dependencies, before any download. Every 30 seconds it computes spend from the start time, the rate locked at rent time, and the bytes received times the per-GB price. It destroys its instance when spend plus a reserve reaches the cap. The reserve is 2 minutes of the hourly rate plus 2 minutes of transfer at the advertised speed. It also destroys the instance if no bytes arrive for 10 minutes during the download.
2. Watchdog credential. On Vast, create an API key whose `api.instance.destroy` permission is limited to the one instance ID. On RunPod, test whether the pod's own `RUNPOD_API_KEY` can delete the pod. Never put the user's full account key on the box. If a backend gives no limited credential, the watchdog stops only the engine, and the quote says the cap on that backend relies on the local check.
3. Local check. While `up`, `status --watch` or `tunnel` runs, the CLI computes spend from marketplace figures, and it destroys the run if the watchdog's heartbeat is more than 3 minutes old.
4. Marketplace deadline. On RunPod, set `terminateAfter` on the deploy request to the time the cap would be reached. Mention it in the quote only after a test shows that RunPod enforces it and deletes the volume.

If the cap can't cover the download, the startup estimate and 10 minutes of use, `up` exits with code 5 and shows the smallest cap that would work. ablit never calls the cap a guarantee.

## Backend drivers

```python
class Backend(Protocol):
    name: str
    def capabilities(self) -> Capabilities: ...
    def search(self, req: HardwareRequest, creds: Creds | None) -> OfferSearch: ...
    def requote(self, offer: Offer, creds: Creds) -> Offer | None: ...
    def create(self, offer: Offer, boot: BootSpec, run_id: str, creds: Creds) -> Allocation: ...
    def reconcile(self, run_id: str, creds: Creds) -> Inventory: ...
    def observe(self, alloc: Allocation, creds: Creds) -> Observation: ...
    def destroy(self, inv: Inventory, creds: Creds) -> TerminationResult: ...
    def verify_billing_stopped(self, inv: Inventory, creds: Creds) -> BillingEvidence: ...
    def watchdog_credential(self, alloc: Allocation, creds: Creds) -> WatchdogCred | None: ...
```

Drivers are modules inside the package, with no plugin discovery. `OfferSearch` says whether the search was complete. `BillingEvidence` holds the check time, the resources still billable, and the final cost if reported. A capability counts as present only after a test shows it works. Offers and drivers have no referral fields. Drivers refuse response shapes and units they don't recognize.

| Backend | v1 | Notes |
|---|---|---|
| Vast | Search and rent | `POST https://console.vast.ai/api/v0/bundles/`. `gpu_name` uses spaces, `gpu_ram` and `cpu_ram` are in MB, `disk_space` is in GB, `compute_cap` is multiplied by 100. Send `rentable: {eq: true}`, `type`, `allocated_storage`, and `order: [["dph_total","asc"]]`. Send the user's key when there is one. |
| RunPod | Search, and rent after its round-trip test | GraphQL `gpuTypes { lowestPrice(input: {gpuCount, minMemoryInGb, ...}) }`. Leave `secureCloud` out for community prices. `uninterruptablePrice` is the total for all GPUs. Rent with `podFindAndDeployOnDemand` and `terminateAfter`, then check the allocated RAM, disk and rate against the quote. |
| Clore | Not in v1 | Bills per minute, with prices per day for the whole machine. Left for a later release. |

## Engine templates

Each engine has `engines/<name>/` with `bootstrap.sh`, `health.sh`, `lock.yaml` and its variable list. `lock.yaml` names the image by digest. All template code is written for this project.

Bootstrap stages, each writing a marker under `/ablit/stages/` so a restart skips finished stages:

1. Start the watchdog and write `/ablit/run.json`.
2. Check GPU count, VRAM and compute capability (`nvidia-smi`), the container memory limit (cgroup `memory.max` and `/proc/meminfo`), the locked-memory limit (`ulimit -l`), and free disk on the data path (`df`). Fail with exit code 6 if any is below the profile.
3. For profiles with `random_read_required`, run a bounded random-read test on a scratch file. Report the result. Set a pass threshold only after a measured passing box.
4. Measure download speed over the first 2 minutes of the largest file. Below 90% of the floor, fail, destroy the run, and add the host to the local deny list. Never pick a replacement host automatically.
5. Download only the listed artifacts at the pinned revision, resuming with range requests and checking that the server honored the range. Check size and hash before renaming each file into place. Keep all state on the data path.
6. Run the staging steps with a checkpoint after each, and delete an input only after its output is verified.
7. Render the engine config with the real port and paths, and start the engine on `127.0.0.1` with a random 32-byte key passed by environment variable.
8. Health check: `GET /v1/models`, one chat completion with `max_tokens: 1`, one streamed completion, and a check that requests with no key or a wrong key are refused.
9. Write `/ablit/ready.json`.

A redaction filter removes the endpoint key and marketplace keys from logs, run files and `--report` output. Tests use fake secrets to check it.

| Engine | Image | Notes |
|---|---|---|
| vLLM | `vllm/vllm-openai`, pinned digest, version 0.31.0 or later | FP8 profile: the DeepSeek V4.1 recipe flags, including `--engram-config '{"cpu_offload":true}'`, `--tensor-parallel-size 8` and `VLLM_ENGINE_READY_TIMEOUT_S=3600`. The planner requires exactly 8 GPUs for it. Key via the `VLLM_API_KEY` environment variable. |
| llama.cpp | `ghcr.io/ggml-org/llama.cpp` server CUDA image, pinned digest | `llama-server -m <file> --host 127.0.0.1 --port <port> --ctx-size 4096 --n-gpu-layers 99`, key via `LLAMA_API_KEY` |
| Xolotl | Built from Xolotl commit `63fc34c` with CUDA 13 and published on GHCR, once the build's dependency licenses are checked. Otherwise built on the box, with the build time in the startup estimate. | Key via the `STRATA_API_KEY` environment variable. The template renders its own config with `/opt/xolotl` and `/models` paths, including the Q2_K draft file. |

## Endpoint

The default is `--endpoint tunnel`. The engine listens on `127.0.0.1` on the box, and ablit opens an SSH port forward that checks the host key and picks a free local port. ablit prints:

```
Endpoint: http://127.0.0.1:<local port>/v1   (through an SSH tunnel to the rented machine)
API key:  saved in your keyring as ablit/<run id>. Run `ablit status <run> --show-key` to print it.
curl http://127.0.0.1:<local port>/v1/chat/completions -H "Authorization: Bearer $ABLIT_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"<served name>","messages":[{"role":"user","content":"Hello"}],"max_tokens":8}'
Closing this tunnel doesn't stop billing. Run `ablit down <run>` to stop it.
```

`--endpoint public` opens the marketplace's mapped port. Before confirmation it prints "This endpoint uses plain HTTP. Your API key and your prompts cross the internet unencrypted."

## Referral links

`ablit setup` lists marketplaces alphabetically and asks "Do you already have an account at <Marketplace>?" A user with an account enters a key, which ablit tests with one read-only call, and sees no referral link. A user without one sees the disclosure and three choices of equal weight: `[1] Sign up with the paid link`, `[2] Sign up directly`, `[3] Skip`. ablit prints links and never opens a browser. Without a project referral ID for a marketplace, only choices 2 and 3 appear.

Vast disclosure:

```
Paid link: if you create a new Vast.ai account through the first link below,
the ablit maintainer earns a commission of 3% of what you spend there, paid by
Vast. ablit adds nothing to your prices. Commissions never affect which machine
ablit picks.
  Sign up (paid link):   https://cloud.vast.ai/?ref_id=<ID>
  Sign up (no referral): https://cloud.vast.ai/
```

The RunPod block uses the same wording with RunPod's terms: 3% of Pod spend and 5% of Serverless spend, paid to the maintainer in RunPod credits for 6 months, and the new account must sign up with Google sign-in. Before each release, check the rates on the program pages and update the date in the README.

Rules:

1. Links appear only in `setup`, only for marketplaces where the user has no account, and only next to the disclosure. A CI test checks every string containing `ref_id=` or `?ref=`.
2. No telemetry.
3. Referral IDs live in `ablit/referral/ids.toml`, which the planner can't import.
4. `ablit setup --no-referral`, `ABLIT_NO_REFERRAL=1` or `referral = false` turns off all referral links, and the setting is saved.
5. The README's "Paid links" section sits next to the setup instructions and gives each rate and the date it was checked.

## Package layout

```
ablit/
  cli.py          # commands and flags
  catalog/        # load, validate, resolve, pre-rent availability check
  select/         # filter chain, cost model, search rule (no referral imports)
  backends/       # vast.py, runpod.py
  engines/        # render templates from engines/<name>/
  runs/           # run files, locks, lifecycle, deny list, tunnel
  referral/       # ids.toml, disclosure strings, setup flow
  watchdog/       # single-file script copied to the box
  redact.py       # secret filtering
```

The repository's top-level `backends/`, `engines/` and `cli/` directories hold the scaffold. When code lands, `cli/` becomes the `ablit/` package, `backends/` notes move into `ablit/backends/`, and `engines/` stays at the top level as data the package reads.

Python 3.11 or newer, installed with `uv tool install ablit` or `pipx install ablit`. Runtime dependencies: `httpx`, `pyyaml`, `pydantic`, `keyring`, `huggingface_hub`. Test dependencies: `pytest`, `hypothesis`, `respx`, `import-linter`.

## Order of work

Every step that rents a machine needs the maintainer's approval of a specific amount and runs on the maintainer's own account. CI never rents.

| Step | Scope | Exit test |
|---|---|---|
| 1 | Scaffold: docs, catalog schema and seed entries, driver notes, engine templates, CLI skeleton, CI | CI passes |
| 2 | `ablit plan` on live Vast and RunPod data, the cost model, the search rule, the quote, the steering tests | `plan` works for every seed entry, and the property tests pass |
| 3 | The 8B llama.cpp entry on Vast with the full run lifecycle, scoped watchdog key, tunnel, `status`, `logs` and `down` | One round trip, one run stopped by the watchdog at a small cap, one recovered lost create response |
| 4 | RunPod renting, `terminateAfter`, the speed check and deny list | One round trip, and a test of whether RunPod enforces `terminateAfter` and deletes the volume |
| 5 | The Xolotl image and the 1017 XQ V3 entry on a 24 GB Vast box | A boot test recording tokens per second and peak disk, which moves the entry to `boot-tested` |
| 6 | The vLLM FP8 entry on 8 GPUs | A boot test on the cheapest qualifying box |
| 7 | Docs, release automation, first PyPI release | `uv tool install ablit` works on a clean machine |

## Left out of v1

Clore renting, launching GGUF files outside the catalog, SkyPilot and dstack export, third-party driver plugins, `--yes` and automatic re-planning, TLS on the public endpoint, any hosted price data or price history, telemetry, multi-node serving, engines other than vLLM, llama.cpp and Xolotl, and a web UI.
