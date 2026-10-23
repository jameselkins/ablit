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
| `ablit up <model>` | Runs `plan`, shows the quote, and asks the user to type `rent`. It then re-queries the offer, writes a local intent record, rents, starts the watchdog, bootstraps the engine, checks health, and prints the endpoint. It rents on Vast, RunPod and Clore. With no `--backends` it searches Vast, and also RunPod and Clore when a key for them exists, and rents where the chosen offer is. With no Vast key it searches only the backends that have a key. Clore renting is connected and untested on a rented machine. |
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
| `--trust any\|verified` | `any` | `verified` keeps only Vast verified hosts and RunPod Secure Cloud. Clore has no verification, so `verified` drops every Clore offer. `any` shows the status without filtering. RunPod Community Cloud offers are dropped from every paid run, with the reason "Community Cloud pods may have no public IP for SSH". |
| `--min-reliability`, `--region`, `--max-price-hr` | none | Optional user filters |
| `--backends vast,runpod,clore` | all enabled backends with keys | Limits the search. Output lists the backends left out and why. |
| `--variant ID` | none | Picks a build when a repo has several |
| `--endpoint tunnel\|public` | `tunnel` | See "Endpoint" |
| `--untested` | off | Required to run `up` on an `unverified` entry |
| `--catalog PATH` | bundled catalog | Local catalog for testing. Paid launch from it also needs `--untested`. |
| `--json` | off | Versioned machine-readable output |

A paid `up` asks the user to type `rent`. `--yes` approves the quote without the prompt, so an agent or a script can rent. The required `--max-spend`, the cap check and the price recheck still apply. A paid `up` with neither a terminal nor `--yes` exits with code 2.

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
2. Put the run ID in the instance label (on RunPod, in the pod name, because a create has no label field). If a create request times out or the response is lost, list the user's instances and look for the label before doing anything else. Never send a second create for the same run.
3. Record every resource ID, including volumes, as soon as it is known.
4. Use a lock file per run. Refuse a second active run of the same model unless the user passes `--another`.
5. `down` moves through `termination_requested`, `resources_terminated` and `billing_stopped_confirmed`. Print "Billing stopped" only in the last state. Otherwise keep the run file, print what is still running and the marketplace page to check, and exit with code 7. Running `down` again retries.
6. Print the final cost only when the marketplace reports it.
7. Start deadline. If the machine is not in the marketplace state "running" 20 minutes after the create (`START_DEADLINE_SECONDS` in `cli/ablit/runs/commands.py`), `up` destroys the run and exits 6 with "the marketplace did not start the machine in time". If cleanup is not confirmed, it exits 7. No path of `up` exits 0 before the engine is ready: a run stopped by another command exits 6, and Ctrl-C before the endpoint exists exits 7 because billing continues. A Vast instance once stayed in "loading" for over 32 minutes. Vast's documentation says GPU charges start when an instance is running and storage charges start at creation (https://docs.vast.ai/api-reference/hello-world, https://docs.vast.ai/cli/reference/show-instances-v1). UNVERIFIED: that nothing at all is billed while an instance loads. Disk charges probably accrue, so the deadline limits them.
8. SSH deadline. Once the marketplace says "running", `up` waits up to `SSH_GRACE_SECONDS` (300) for the bootstrap upload to succeed. The clock starts at the first pass that sees "running" and does not depend on an SSH call raising. Every SSH call has a timeout, and a timeout is treated like a failed call. ssh runs in its own process group, and the group is killed on a timeout, so a helper process that holds the output pipes cannot keep `up` waiting. `up` prints "waiting for SSH on the machine (N s)" on every pass. When the time is up it prints a line, adds the host to the deny list with the reason "SSH did not answer after the machine was running", runs `down`, and exits 6, or 7 if cleanup is not confirmed. Five minutes is kept because sshd normally answers within a minute or two, and each extra minute bills.
9. Lock use. `up` takes the run lock for one check at a time, and waits up to 10 seconds for it. If another command holds it longer, `up` skips that check and carries on. `status` never waits for the lock. When the lock is busy it prints the saved record and says so on standard error, without asking the marketplace.

State lives in the platform's user state directory. There is no database and no daemon.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Unexpected error while no run is billing |
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
  - {step: join, inputs: [weight], output_bytes: ..., keeps_inputs_until_done: false, delete_inputs_after: true}
profile:
  engine: xolotl
  engine_lock: xolotl-03be8a6
  gpu: {vendor: nvidia, count: 1, vram_gb_each: 24, compute_capability_min: 7.5}
  host_ram_gb: 90
  host_ram_gb_recommended: 256
  storage: {random_read_required: true}
  context_tokens: 4096
  concurrency: 1
  port: 8150
  startup_hours_estimate: 0.3
  min_download_mbps: 1000
sources: [url, ...]
tests: []
status: unverified                 # unverified | boot-tested | removed
```

Rules for the schema:

1. `artifacts` lists every file the engine reads, including draft models and tokenizer files.
2. Large files carry the Hugging Face LFS SHA-256. Small files without an LFS record carry the Git blob ID, and `verify_entry.py` downloads them at the pinned revision to compute a SHA-256.
3. Peak disk is computed from `staging`, never written by hand. The Xolotl staging has no Engram extraction step, because the engine reads the two official Engram shards in place (see "Xolotl download and overlap" below). Its `join` step says `keeps_inputs_until_done: false`, because the bootstrap appends one weight part at a time and deletes it, so the peak is the whole download and not twice the weights. The engine lock adds a margin of one weight part on top of the peak (`disk_margin_gb`), and room for the fallback Engram extraction (`extract_disk_gb`, 210 GB), which costs a few cents an hour.
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
| `ds41-september-gsq-uncensored` | Xolotl | One 24 GB NVIDIA GPU of compute capability 8.6, 8.9 or 12.0, a driver of version 580 or newer, 90 GB host RAM (256 GB recommended), a 1000 Mbps download floor, disk from staging (about 604 GB asked for, of which 372 GB is the staging peak) | Floors from the Xolotl README, not yet tested. Untested on a rented machine. Best agentic scores among the single-GPU builds, by the author's own tests. |
| `ds41-1017-xq-v3-uncensored` | Xolotl | Same | Best 1017 build by the author's own scores. Untested on a rented machine. V2 was removed on 2026-10-21. |
| `llama33-70b-fp8-dynamic` | vLLM | From its sources | 70B-class vLLM build |
| `llama31-8b-abliterated-q4km` | llama.cpp | From its sources | First paid test target |

## Planner

### Filter chain, in order

The planner records how many offers each step removed.

1. Engine floor: GPU vendor, count, VRAM per GPU and in total, compute capability, host RAM and OS. When the engine lock lists `cuda_architectures`, the compute capability must be one of the listed generations, because the image holds code for those and no others. On Vast, the search also sends `cuda_max_good >= cuda_version_min` (the highest CUDA version the host driver supports). The quote shows the GPU model.
2. Disk floor: free disk at least the staging peak plus the engine image plus the lock's `disk_margin_gb` and `extract_disk_gb`. On Vast, send `disk_space >= N` and `allocated_storage = N` so the price includes the disk.
3. Download speed floor: Vast's per-host `inet_down` must meet the floor. RunPod reports no per-host speed, so its offers are kept, labeled "speed not advertised, measured after boot", and ranked as if they ran exactly at the floor.
4. Rental type and availability: on demand unless `--spot`, Vast `rentable = true`, RunPod `stockStatus` not null, Clore `rented = false` with every GPU free (ablit rents whole Clore servers only), and remaining rental duration at least the startup estimate plus `--hours`. For Clore the duration is the host's `mrl`, in hours.
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

`hourly_all_in` is Vast `dph_total` at the needed `allocated_storage`, or RunPod `uninterruptablePrice` for all GPUs plus volume disk, or Clore's USD price per day for the whole server divided by 24 and raised by the renter fee of 5%. Clore's one-time order fee of $0.10 goes in `fixed_fees`. `bandwidth_cost_per_gb` is Vast's `inet_down_cost`, and zero for RunPod and Clore. The quote shows `D` and `S` on separate lines and labels `S` an estimate.

`S` counts only the time after the last downloaded byte, because `D` already counts the download. The Xolotl entries use `S = 0.3` hours, and the figure comes from this sum.

```
image pull, container start and health check   0.25 hours   the startup estimate of the boot-tested llama.cpp entry
engine load                                     92 seconds    the longest load time on the model cards
last join and checksum after the final byte     60 seconds    an allowance, because the join runs during the download
total                                           0.292 hours   rounded up to 0.3
```

None of the three terms has been measured on the Xolotl image. The first boot test replaces them with a measured figure.

### Search rule

Vast returns at most 64 rows per query, so the first 64 rows by hourly price can miss the lowest total. Use this rule:

1. Query with all filters on the server, ordered by the GPU-only price `dph_base` ascending. Vast applies a `dph_total` filter to the GPU-only price, not to the all-in price, so paging on `dph_total` would skip offers. Paging therefore uses `dph_base`.
2. Read rows into a buffer. A row is final once its all-in price `dph_total` is below the `dph_base` of the last row read, because every unread row has a GPU-only price at least that high and an all-in price at least as high. Hand final rows to the planner in all-in order.
3. Compute `total(H)` for each row handed over, and keep the lowest.
4. No unseen offer can cost less than `last_all_in * (H + S)`, where `last_all_in` is the all-in price of the last row handed over.
5. If the lowest total is at or below that bound, stop.
6. Otherwise query again with `dph_base >= last_base` and drop rows already seen. If Vast refuses that query, returns rows below the cursor, or returns a full page at one GPU-only price, or after 10 queries, stop and label the result "cheapest of the N offers checked".

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
2. Watchdog credential. On Vast, create an API key whose `api.instance.destroy` permission is limited to the one instance ID. Never put the user's full account key on the box. If a backend gives no limited credential, the quote says the cap on that backend relies on the local check. RunPod documents no key limited to one pod, and its API has no call that creates a key, so the RunPod driver returns none. The watchdog on a RunPod pod still tries to delete its own pod with the `RUNPOD_API_KEY` that RunPod puts in the pod, which RunPod calls pod-scoped. What that key may do is undocumented and untested, so nothing relies on it. `ablit plan` and `ablit up` add a note to the spend cap notice when the chosen offer is on RunPod or Clore. See `backends/runpod.md`. Clore documents no limited key (its keys give full access to the account), so the Clore driver returns none, and a Clore run is capped only by the local check and by the host's rental length limit, which can be up to 3000 hours.
3. Local check. While `up`, `status --watch` or `tunnel` runs, the CLI computes spend from marketplace figures, and it destroys the run if the watchdog's heartbeat is more than 3 minutes old. On a RunPod or Clore run the heartbeat age is ignored, because the machine cannot be trusted to destroy itself.
4. Marketplace deadline. RunPod's v2 create request has no deadline field (`terminateAfter` was a GraphQL option), so no backend has one today. Clore has none either. For every backend ablit applies its own start deadline of 20 minutes from the create. If the machine is not `running` by then, ablit cancels the run and exits 6 once the cancel is confirmed, or 7 when it is not. On Clore, a run that reaches the deadline with no public SSH port also puts the server on the local deny list.

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

Drivers are modules inside the package, with no plugin discovery. A driver that rents machines subclasses `RentalBackend` in `backends/rental.py`, which holds the HTTP client, the create journal that allows one create per run, the destroy loop and the billing check. The retry, pacing and redirect helpers are in `backends/transport.py`. A new driver writes only its marketplace's calls and field mapping. `OfferSearch` says whether the search was complete. `BillingEvidence` holds the check time, the resources still billable, and the final cost if reported. A capability counts as present only after a test shows it works. Offers and drivers have no referral fields. Drivers refuse response shapes and units they don't recognize.

| Backend | v1 | Notes |
|---|---|---|
| Vast | Search and rent | `POST https://console.vast.ai/api/v0/bundles/`. `gpu_name` uses spaces, `gpu_ram` and `cpu_ram` are in MB, `disk_space` is in GB, `compute_cap` is multiplied by 100. Send `rentable: {eq: true}`, `type`, `allocated_storage`, and `order: [["dph_total","asc"]]`. Send the user's key when there is one. |
| RunPod | Search, and a rental lifecycle that is untested on a rented machine. `ablit up` accepts `--backends runpod`. | Search uses GraphQL `gpuTypes { lowestPrice(input: {gpuCount, minMemoryInGb, ...}) }`. Leave `secureCloud` out for community prices, which `plan` shows only as dropped, because Community Cloud pods may have no public IP. `uninterruptablePrice` is the total for all GPUs. Renting uses REST API v2 at `https://api.runpod.io/v2` (v1 retires on 2026-11-15): `POST /v2/pods` with the pod named `ablit-<run id>`, `GET /v2/pods` to find, watch and check billing, and `DELETE /v2/pods/{id}` to terminate. A stopped pod still bills for its volume, so ablit never stops one. The key goes in `Authorization: Bearer`. SSH is the pod's direct TCP mapping of port 22 (the proxy cannot forward ports), with trust on first use for the host key. After the create, the driver checks the pod's GPU, RAM, disk, cloud and cost against the quote. See `backends/runpod.md` for each call, its source and what is unverified. |
| Clore | Search, and a rental lifecycle that `ablit up` now uses. Renting is untested on a rented machine, and the driver's `rent` flag stays off until a paid test passes. | `GET https://api.clore.ai/v1/marketplace` lists every server with no filters, and works without a key although the docs say it needs one. Prices are USD per day for the whole server, and ablit reads only the `USD-Blockchain` price. The renter pays 5% on top (half of the 10% base fee), plus a $0.10 order fee that Clore charges only when the order lasts more than 10 minutes. Billing is per minute. The key goes in a header named `auth`, at most 1 request per second. Create with `POST create_order`, find the order again in `GET my_orders` (the create call returns no label), stop it with `POST cancel_order`. See `backends/clore.md` for each call and its doc page. |

## Engine templates

Each engine has `engines/<name>/` with `bootstrap.sh`, `health.sh`, `lock.yaml` and its variable list. `lock.yaml` names the image by digest. All template code is written for this project.

Bootstrap stages, each writing a marker under `/ablit/stages/` so a restart skips finished stages:

1. Start the watchdog and write `/ablit/run.json`.
2. Check GPU count, VRAM and compute capability (`nvidia-smi`), the container memory limit (cgroup `memory.max` and `/proc/meminfo`), the locked-memory limit (`ulimit -l`), and free disk on the data path (`df`). Fail with exit code 6 if any is below the profile.
3. For profiles with `random_read_required`, run a bounded random-read test on a scratch file. Report the result. Set a pass threshold only after a measured passing box.
4. Measure download speed with the real download client. The Xolotl bootstrap runs it for 45 seconds on the first files of the real list and keeps those bytes. The measure is the larger of the average rate and the rate over the last 10 seconds, and it must reach 90% of the floor. Below that, fail, destroy the run, and add the host to the local deny list. Never pick a replacement host automatically.
5. Download only the listed artifacts at the pinned revision with `engines/lib/fetch.py`, which resumes each range, checks that the server honored the range, and hashes while it downloads. A file is renamed into place only after its size and SHA-256 match, and a mismatch fails the run. Keep all state on the data path.
6. Run the staging steps with a checkpoint after each, and delete an input only after its output is verified. For the Xolotl entries, the join and the Engram work start during stage 5 and finish in stage 6.

Progress goes to `/ablit/progress/download.json`, `join.json` and `extract.json`, each holding `bytes_done`, `bytes_total`, `mbps`, `files_done`, `files_total` and a time. The status probe reads them, `ablit status` shows them in the run record, and `ablit up` prints a line each time a step passes another ten percent. The CLI keeps only the known steps and numeric fields, and a malformed file is dropped.
7. Render the engine config with the real port and paths, and start the engine on `127.0.0.1` with a random 32-byte key passed by environment variable.
8. Health check: `GET /v1/models`, one chat completion with `max_tokens: 1`, one streamed completion, and a check that requests with no key or a wrong key are refused.
9. Write `/ablit/ready.json`.

A redaction filter removes the endpoint key and marketplace keys from logs, run files and `--report` output. Tests use fake secrets to check it.

### Xolotl download and overlap

The Xolotl bootstrap exists to keep the rented machine busy from the first minute, because the 372 GB download is most of the bill. None of it has run on a rented machine yet, so every figure below is a design value and not a measurement.

The download client is `engines/lib/fetch.py`, which uses only the Python standard library, so it also runs in the llama.cpp image. It works as follows.

1. A file of 512 MiB or more is cut into chunks of 256 MiB. Each chunk is one HTTP range request, and the client rejects any answer that is not 206 Partial Content, so a server that ignores the range cannot write wrong bytes.
2. Two large files download at once, with up to 16 chunks of each in flight, which is 32 connections. Small files download 8 at a time with one range each. The values are the settings `ABLIT_DOWNLOAD_FILES`, `ABLIT_DOWNLOAD_RANGES`, `ABLIT_DOWNLOAD_CHUNK_BYTES` and `ABLIT_DOWNLOAD_SPLIT_BYTES`.
3. A failed chunk is retried with growing waits, and it resumes from the bytes already written. A chunk that makes no progress for the stall time is dropped and retried.
4. One thread per file hashes the finished beginning of the file while the later chunks still download, so a file needs no second read. The file is renamed into place only when its size and SHA-256 match the catalog, and a mismatch fails the run with the reason `hash_mismatch`.
5. The state of each part is kept next to it, so a restart resumes at the chunk level and never repeats a verified file.

The order of work for the default mode (`ABLIT_ENGRAM_MODE=direct`) is as follows.

1. The speed check in stage 4 runs the real client with the real connections on the first weight parts for 45 seconds, and the bytes it downloads are kept.
2. Stage 5 downloads the small files, which are the index, tokenizer, rank, config, expert map, checksum and script files.
3. The weight parts and the draft model download next, continuing where the speed check stopped. A join worker started before the download appends each part to `joined/model.gguf` as soon as the part is verified, then deletes it, so the join finishes a few seconds after the last part.
4. The two Engram source files download last. They need no work after they are verified, so the end of the download has only the hash of the final chunks behind it.
5. The engine starts when every file is verified and joined. It reads the two official Engram files in place.

With `ABLIT_ENGRAM_MODE=extract`, the two Engram source files come first, and an extraction worker starts as soon as both are verified. It runs `extract_engram.py` while the weight parts download, and it needs about 203 GB of extra disk, which the disk request already includes. The extraction also runs as an automatic fallback when the engine exits at start with an Engram open error in direct mode.

Direct mode reads the Xolotl source at the pinned commit. `Ds41Engram::open` takes the folder that holds `model.safetensors.index.json` and the shards, and the upstream Windows notes say the shards are read in place. It is not run on Linux, so the default may fall back to extraction on the first boot test.

The pre-extracted Engram tables in `kaushikvira/DeepSeek-V4.1-Flash-1017-GSQ-agentic-strata` are not used. Their headers, sizes and index match a regeneration from the official 1017 shards, and 602 sampled 1 MiB blocks per tensor were identical, but the check was not a hash of all 203 GB. The September shards differ from the 1017 shards in every sampled block, so the September entry cannot use them. Direct mode already removes the copy, so the tables would add a third party to the trust path and save nothing.

| Engine | Image | Notes |
|---|---|---|
| vLLM | `vllm/vllm-openai`, pinned digest, version 0.31.0 or later | FP8 profile: the DeepSeek V4.1 recipe flags, including `--engram-config '{"cpu_offload":true}'`, `--tensor-parallel-size 8` and `VLLM_ENGINE_READY_TIMEOUT_S=3600`. The planner requires exactly 8 GPUs for it. Key via the `VLLM_API_KEY` environment variable. |
| llama.cpp | `ghcr.io/ggml-org/llama.cpp` server CUDA image, pinned digest | `llama-server -m <file> --host 127.0.0.1 --port <port> --ctx-size 4096 --n-gpu-layers 99`, key via `LLAMA_API_KEY` |
| Xolotl | `ghcr.io/jameselkins/ablit-xolotl`, tag `03be8a6` (the Xolotl commit), built with CUDA 13 for compute capability 8.6, 8.9 and 12.0 by `.github/workflows/xolotl-image.yml`, pinned by digest in `engines/xolotl/lock.yaml`. The image holds no model files and no compiler, so nothing is built on the rented machine. The package must be public before a rental, because Vast pulls without credentials. | Key via the `STRATA_API_KEY` environment variable. The template renders its own config with `/opt/xolotl` and `/models` paths, including the Q2_K draft file, and starts `serve_ds41.py` with the `strata` engine. The paths sit in one block at the top of `engines/xolotl/bootstrap.sh`. Untested on a rented machine. |

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
the ablit maintainer earns 3% of what you spend there for the life of your
account, paid by Vast as credits. ablit adds nothing to your prices.
Commissions never affect which machine ablit picks. To hide paid links, run
`ablit setup --no-referral`.
  Sign up (paid link):   https://cloud.vast.ai/?ref_id=<ID>
  Sign up (no referral): https://cloud.vast.ai/
```

The RunPod block uses the same wording with RunPod's terms: 3% of Pod spend and 5% of Serverless spend, paid to the maintainer in RunPod credits for 6 months, and the new account must sign up with Google sign-in. The Clore block uses Clore's terms from its referral program page: a share of the fees Clore charges the new account, 15% as standard and 50% during a promotion that Clore calls limited-time and gives no end date for, paid by Clore from its own fee. That is about 0.7% and 2.4% of spend at the 5% renter fee. The page says nothing about how long a referral lasts, and the block says so. The Clore ID is empty until the maintainer has a Clore account, so only the direct signup and skip choices appear for now. All blocks end with the `--no-referral` hint. Before each release, check the rates on the program pages and update the date in the README.

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
  backends/       # vast.py, runpod.py, clore.py
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
| 4 | RunPod and Clore renting (written, with synthetic tests; Clore is connected to `ablit up`), the speed check and deny list | One round trip on RunPod, a measurement of `ulimit -l` inside a pod, and a test of whether the pod's own key can delete the pod, and one round trip on Clore (whether the pinned image digest is accepted, and whether `command` runs with `autossh_entrypoint`) |
| 5 | The Xolotl image and the 1017 XQ V3 entry on a 24 GB Vast box | A boot test recording tokens per second and peak disk, which moves the entry to `boot-tested` |
| 6 | The vLLM FP8 entry on 8 GPUs | A boot test on the cheapest qualifying box |
| 7 | Docs, release automation, first PyPI release | `uv tool install ablit` works on a clean machine |

## Left out of v1

Launching GGUF files outside the catalog, SkyPilot and dstack export, third-party driver plugins, automatic re-planning, TLS on the public endpoint, any hosted price data or price history, telemetry, multi-node serving, engines other than vLLM, llama.cpp and Xolotl, and a web UI.
