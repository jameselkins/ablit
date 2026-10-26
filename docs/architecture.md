# Architecture

This page explains how ablit works inside. It is for contributors and for anyone who wants to check what ablit does with their money and keys. For command flags, read [cli-reference.md](cli-reference.md). For the reasons behind the main choices, read [design-decisions.md](design-decisions.md).

## Overview

ablit is a command-line tool that runs on the user's computer with the user's own marketplace accounts. Given a model, it finds the cheapest rented GPU machine that meets the model's needs, shows the full cost, and asks the user to confirm. It then rents the machine, starts the inference engine, downloads the weights, checks that the engine answers, and prints an OpenAI-compatible endpoint.

ablit runs no service, holds no money and relays no inference traffic. State lives in the platform's user state directory, and there is no database and no daemon.

## Package layout

The Python package is `cli/ablit/`. The catalog, the engine templates and the backend notes sit at the top of the repository, and the wheel carries copies of the catalog and the engine templates in `ablit/_data`.

```
cli/ablit/
  cli.py          commands and flags
  credentials.py  keys from the environment, the OS keyring or process memory
  plan.py         the plan command and its JSON output
  catalog/        load, validate, resolve and compute staging for catalog entries
  select/         filter chain, cost model, search rule (cannot import referral)
  backends/       vast.py, runpod.py, clore.py, lium.py
  engines/        renders the engine files and configuration for the machine
  runs/           run files, locks, lifecycle, deny list, SSH and tunnel
  referral/       ids.toml, disclosure text, setup choices
  watchdog/       ablit_watchdog.py, a single file copied to the machine
  redact.py       secret filtering
catalog/          models/*.yaml, schema/v1.json, boot-tests/
engines/          vllm, llamacpp and xolotl templates, plus lib/ with shared scripts
backends/         notes on each marketplace API, with sources
tools/            verify_entry.py and build_catalog.py
```

Python 3.11 or newer is required. Runtime dependencies are `httpx`, `pyyaml`, `pydantic`, `keyring` and `huggingface_hub`. Test dependencies are `pytest`, `hypothesis`, `respx` and `import-linter`.

## Run lifecycle

The code for these steps is in `cli/ablit/runs/`. The first four steps keep ablit from losing track of something it is paying for.

1. Before creating anything, `up` renders the bootstrap bundle to check that the installed package has all its files. It then writes `runs/<run-id>.json` with the approved quote and state `creating`. It writes through a temporary file, renames it, and flushes to disk.
2. The run ID goes in the instance label. On RunPod it goes in the pod name, because a create has no label field. If a create request times out or the response is lost, ablit lists the user's instances and looks for the label before doing anything else, and it never sends a second create for the same run.
3. Every resource ID, including volumes, is recorded as soon as it is known.
4. A lock file per run protects the run file. A second active run of the same model is refused unless the user passes `--another`. `up` resumes an existing run only when it is still `creating`, its marketplace is one this `up` searches, the cap and build match, and no live `up` holds the run's owner lock. Each `up` holds that lock from the moment it saves or resumes the run until it exits, so two live `up` processes never share a run. The rules are in `resumable` in `cli/ablit/runs/up.py`.
5. `down` moves through `termination_requested`, `resources_terminated` and `billing_stopped_confirmed`. It prints "Billing stopped" only in the last state. Otherwise it keeps the run file, prints what is still running and the marketplace page to check, and exits with code 7. Running `down` again retries.
6. The final cost is printed only when the marketplace reports it.
7. Start deadline. If the machine is not in the marketplace state "running" 20 minutes after the create (`START_DEADLINE_SECONDS` in `cli/ablit/runs/monitor.py`), `up` destroys the run and exits 6, or 7 if cleanup is not confirmed. No path of `up` exits 0 before the engine is ready. A run stopped by another command exits 6, and Ctrl-C before the endpoint exists exits 7 because billing continues. A Vast instance once stayed in "loading" for over 32 minutes. Vast's documentation says GPU charges start when an instance is running and storage charges start at creation, and it is not verified whether anything is billed while an instance loads. Disk charges probably accrue, so the deadline limits them.
8. SSH deadline. Once the marketplace says "running", `up` waits up to `SSH_GRACE_SECONDS` (300) for the bootstrap upload to succeed. The clock starts at the first pass that sees "running" and does not depend on an SSH call raising. Every SSH call has a timeout, ssh runs in its own process group, and the group is killed on a timeout, so a helper process that holds the output pipes cannot keep `up` waiting. When the time is up, ablit adds the host to the deny list with the reason "SSH did not answer after the machine was running", runs `down`, and exits 6, or 7 if cleanup is not confirmed. Five minutes is enough because sshd normally answers within a minute or two, and each extra minute bills.
9. Lock use. While it monitors a run, `up` takes the run lock for one check at a time and waits up to 10 seconds for it. If another command holds it longer, `up` skips that check. `status` never waits for the lock. When the lock is busy it prints the saved record, says so on standard error, and does not ask the marketplace.

The host deny list stores IDs under their marketplace, so a failed host on one marketplace cannot exclude a host on another. Older files are read using the marketplace in matching saved run records. Without a matching run, a UUID is treated as a Lium host and other IDs as Vast hosts. The next write saves the new format. If an old ID matches runs on several marketplaces, its denial is kept on each one because the original file does not identify which failed.

## Planner

The planner is in `cli/ablit/select/`. Its ranked list is a function only of the offers, the catalog profile and the user's flags.

### Filter chain

The planner applies these steps in order and records how many offers each step removed.

1. Engine floor. GPU vendor, count, VRAM per GPU and in total, compute capability, host RAM and OS. When the engine lock lists `cuda_architectures`, the compute capability must be one of the listed generations, because the image holds code for those and no others. On Vast the search also sends a minimum for the highest CUDA version that the host driver supports.
2. Disk floor. Free disk must be at least the staging peak, plus the engine image, plus the lock's `disk_margin_gb` and `extract_disk_gb`. On Vast the search sends `disk_space >= N` and `allocated_storage = N`, so the price includes the disk.
3. Download speed floor. Vast's per-host `inet_down` must meet the floor. RunPod reports no per-host speed, so its offers are kept, labeled "speed not advertised, measured after boot", and ranked as if they ran exactly at the floor.
4. Rental type and availability. Offers are on demand, Vast `rentable = true`, RunPod `stockStatus` not null, and Clore `rented = false` with every GPU free, because ablit rents whole Clore servers only. The remaining rental duration must be at least the startup estimate plus `--hours`. For Clore the duration is the host's `mrl`, in hours.
5. User filters, only when set.
6. Rank by total cost, using the search rule below.

Ties break on backend name, then on offer ID, alphabetically.

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

`hourly_all_in` is Vast `dph_total` at the needed `allocated_storage`, RunPod `uninterruptablePrice` for all GPUs plus volume disk, or Clore's USD price per day for the whole server divided by 24 and raised by the renter fee of 5%. Clore's one-time order fee of $0.10 goes in `fixed_fees`. `bandwidth_cost_per_gb` is Vast's `inet_down_cost`, and it is zero for RunPod and Clore. The quote shows `D` and `S` on separate lines and labels `S` an estimate. CONTRIBUTING.md explains how an entry's startup estimate is derived.

### Search rule

Vast returns at most 64 rows per query, so the first 64 rows by hourly price can miss the lowest total. The search code is in `cli/ablit/select/search.py`, and it follows this rule.

1. Query with all filters on the server, ordered by the GPU-only price `dph_base` ascending. Vast applies a `dph_total` filter to the GPU-only price, not to the all-in price, so paging on `dph_total` would skip offers.
2. Read rows into a buffer. A row is final once its all-in price `dph_total` is below the `dph_base` of the last row read, because every unread row has a GPU-only price at least that high and an all-in price at least as high. Final rows go to the planner in all-in order.
3. Compute `total(H)` for each row handed over, and keep the lowest.
4. No unseen offer can cost less than `last_all_in * (H + S)`, where `last_all_in` is the all-in price of the last row handed over.
5. If the lowest total is at or below that bound, stop.
6. Otherwise query again with `dph_base >= last_base` and drop rows already seen. If Vast refuses that query, returns rows below the cursor, or returns a full page at one GPU-only price, or after 10 queries, stop and label the result "cheapest of the N offers checked".

If a backend fails or times out, the output names it and says the comparison did not include it.

The report is incomplete if any requested backend fails or returns an incomplete search. `plan` still shows the offers it found, and `up` refuses an incomplete quote with exit code 5 before registering a key or renting a machine.

### Steering by commission

Referral data is not an input to search, filtering or ranking. An import-linter contract in `.importlinter` stops `ablit.select` and `ablit.backends` from importing `ablit.referral`, and tests in `cli/tests/test_steering.py` check the result. POLICY.md states the rule and lists the tests.

## Spend cap

The cap has four layers, and none of them is a guarantee.

1. Watchdog on the machine. The bootstrap starts `ablit-watchdog`, a single Python file with no dependencies, before any download. Every 30 seconds it computes spend from the start time, the rate locked at rent time, and the bytes received times the per-GB price. It destroys its instance when spend plus a reserve reaches the cap. The reserve is 2 minutes of the hourly rate plus 2 minutes of transfer at the advertised speed. It also destroys the instance if no bytes arrive for 10 minutes during the download.
2. Watchdog credential. On Vast, ablit creates an API key whose `api.instance.destroy` permission is limited to the one instance ID, and it never puts the user's full account key on the machine. RunPod and Clore document no key limited to one machine, so their drivers return none, and the quote says the cap on those backends relies on the local check. See `backends/runpod.md` and `backends/clore.md` for the details and for what is unverified.
3. Local check. While `up`, `status --watch` or `tunnel` runs, the CLI computes spend from marketplace figures and the watchdog's reported spend. It destroys the run when estimated spend plus the reserve reaches the cap, regardless of the heartbeat's age. A watchdog that is still sending heartbeats may be unable to destroy the machine, so the local check enforces the cap independently.
4. Start deadline. No marketplace offers a start deadline field that ablit can use today, so ablit applies its own start deadline of 20 minutes from the create. Lium's `termination_hours` is a different deadline, which deletes the pod a whole number of hours after the rent, and ablit sets it from the cap. On Clore, a run that reaches the deadline with no public SSH port also puts the server on the local deny list.

If the cap cannot cover the download, the startup estimate and 10 minutes of use, `up` exits with code 5 and shows the smallest cap that would work.

## Backend drivers

Drivers are modules inside the package, with no plugin discovery. A driver that rents machines subclasses `RentalBackend` in `backends/rental.py`, which holds the HTTP client, the create journal that allows one create per run, the destroy loop and the billing check. The retry, pacing and redirect helpers are in `backends/transport.py`. A new driver writes only its marketplace's calls and field mapping. Each one implements the protocol in `cli/ablit/backends/base.py`, which has these methods: `capabilities`, `search`, `requote`, `create`, `reconcile`, `observe`, `destroy`, `verify_billing_stopped` and `watchdog_credential`. `OfferSearch` says whether the search was complete, and `BillingEvidence` holds the check time, the resources still billable, and the final cost if reported. A capability counts as present only after a test shows it works. Offers and drivers have no referral fields, and drivers refuse response shapes and units they do not recognize.

Each marketplace has a note under `backends/` that lists every API call, its documentation page, and what is unverified.

| Backend | Search | Renting |
|---|---|---|
| Vast, see [backends/vast.md](../backends/vast.md) | Yes | Tested on a rented machine |
| RunPod, see [backends/runpod.md](../backends/runpod.md) | Yes | Tested on Secure Cloud only |
| Clore, see [backends/clore.md](../backends/clore.md) | Yes | Connected to `ablit up`, and untested on a rented machine |
| Lium, see [backends/lium.md](../backends/lium.md) | Yes | Tested on a rented machine on 2026-10-26 |

## Engine templates

Each engine has a folder `engines/<name>/` with `bootstrap.sh`, `health.sh` and `lock.yaml`. The lock names the image by digest, and ablit refuses to rent when the digest is null. All template code is written for this project. The shared scripts are `engines/lib/common.sh` and the download client `engines/lib/fetch.py`.

| Engine | Image | Notes |
|---|---|---|
| llama.cpp | `ghcr.io/ggml-org/llama.cpp` server CUDA image, pinned by digest | Key through `LLAMA_API_KEY` |
| Xolotl | `ghcr.io/jameselkins/ablit-xolotl`, built by `.github/workflows/xolotl-image.yml` and pinned by digest in `engines/xolotl/lock.yaml` | Key through `STRATA_API_KEY`. The image holds no model files and no compiler. The package must be public before a rental, because Vast pulls without credentials. See `engines/xolotl/image/README.md`. |
| vLLM | `vllm/vllm-openai`, version 0.31.0 or later | The digest is not pinned yet, so `ablit up` refuses vLLM entries. Key through `VLLM_API_KEY`. |

### Bootstrap stages

The bootstrap runs nine stages. Each writes a marker under `/ablit/stages/`, so a restart skips finished stages.

1. Start the watchdog and write `/ablit/run.json`.
2. Check GPU count, VRAM and compute capability (`nvidia-smi`), the container memory limit, the locked-memory limit (`ulimit -l`), and free disk on the data path. Fail with exit code 6 if any is below the profile.
3. For profiles with `random_read_required`, run a bounded random-read test on a scratch file and report the result.
4. Measure download speed with the real download client. The Xolotl bootstrap runs it for 45 seconds on the first files of the real list and keeps those bytes. The measure is the larger of the average rate and the rate over the last 10 seconds, and it must reach 90% of the floor. Below that, the run fails, ablit destroys it, and the host goes on the local deny list. ablit never picks a replacement host automatically.
5. Download only the listed artifacts at the pinned revision with `engines/lib/fetch.py`.
6. Run the staging steps with a checkpoint after each, and delete an input only after its output is verified.
7. Render the engine config with the real port and paths, and start the engine on `127.0.0.1` with a random 32-byte key passed by environment variable.
8. Health check. `GET /v1/models`, one chat completion with `max_tokens: 1`, one streamed completion, and a check that requests with no key or a wrong key are refused.
9. Write `/ablit/ready.json`.

Progress goes to `/ablit/progress/download.json`, `join.json` and `extract.json`. Each holds `bytes_done`, `bytes_total`, `mbps`, `files_done`, `files_total` and a time. `ablit status` shows them in the run record, and `ablit up` prints a line each time a step passes another ten percent. The CLI keeps only the known steps and numeric fields, and drops a malformed file.

A redaction filter removes the endpoint key and marketplace keys from logs, run files and output. Tests use fake secrets to check it.
