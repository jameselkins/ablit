# Vast driver notes

These notes describe what the Vast driver sends and reads. The search code in `cli/ablit/backends/vast.py` was checked on 2026-10-20 with anonymous, read-only search requests, and the lifecycle code now has synthetic tests. No paid check has run yet. The driver refuses a response shape or unit it does not recognize.

## Search

Endpoint: `POST https://console.vast.ai/api/v0/bundles/`

The request body is a JSON object of filters. It follows these conventions.

1. `gpu_arch` is the GPU vendor, for example "nvidia". The driver does not filter by GPU model, because a catalog entry names no model. The names Vast reports for a GPU, such as "RTX 4090", are shown in the quote.
2. `gpu_ram`, `gpu_total_ram` and `cpu_ram` are in megabytes. The driver converts a gigabyte floor to megabytes by multiplying by 1000, because a card sold as 24 GB reports 24564 MB and would fail a floor of 24 GB if the driver divided by 1024.
3. `disk_space` is in gigabytes.
4. `compute_cap` is the compute capability multiplied by 100, so 7.5 is sent as 750. When an engine image holds code for only some GPU generations, the lock lists them in `cuda_architectures` (compute capability times ten), and the search sends the smallest one as the floor, for example 860 for 8.6. Vast filters only from below, so the planner removes offers with other generations after the search, with the reason "GPU generation is not in the engine image". The field and its operators (`eq`, `neq`, `gt`, `lt`, `gte`, `lte`, `in`, `notin`) are in the [search offers reference](https://docs.vast.ai/api-reference/search/search-offers).
5. The body includes `cpu_arch: {eq: "amd64"}` for the engine images. It also always includes `rentable: {eq: true}`, `type: "on-demand"`, `gpu_frac: {eq: 1}` (so hosts that share one GPU between renters are left out), and `allocated_storage` set to the disk the run needs. Together with `disk_space >= N`, this makes the quoted price include the disk. A live check showed that the price in each row covers exactly the allocated size.
6. `num_gpus`, `inet_down` and `duration` (the remaining rental time in seconds) carry the GPU count, the download floor and the startup estimate plus `--hours`.
7. `cuda_max_good` is the highest CUDA version the host driver supports, which the search reference lists as "Maximum supported CUDA version" and the command line reference lists as `cuda_vers` ([search offers](https://docs.vast.ai/api-reference/search/search-offers), [command line reference](https://docs.vast.ai/cli/reference/search-offers)). When the engine lock sets `cuda_version_min`, the body sends `cuda_max_good: {gte: <that version>}`. The Xolotl image is built on CUDA 13.0, which needs a driver of the R580 branch or newer ([CUDA toolkit release notes](https://docs.nvidia.com/cuda/cuda-toolkit-release-notes/index.html)), so its lock asks for 13.0. The bootstrap checks the driver again with `nvidia-smi` before it downloads anything.
8. The user's filters are sent too: `verified`, `reliability`, `geolocation` (with the two-letter country code) and an upper bound on `dph_base` for `--max-price-hr`.
9. The body orders results with `order: [["dph_base", "asc"]]`, and a later page adds `dph_base: {gte: <last GPU-only price>}`. See "Search rule" for the reason.

The driver sends the user's key in an `Authorization: Bearer` header only when one is set in `VAST_API_KEY`. Anonymous searches work.

## Reading offers

| Field | Used for |
|---|---|
| `dph_total` | The all-in hourly price at the needed `allocated_storage`. |
| `dph_base` | The GPU-only price, shown separately in the quote and used to page. |
| `inet_down` | Advertised download speed per host. Checked against the speed floor. |
| `inet_down_cost` | Transfer price per GB. Counted in the total cost. |
| `rentable` | Must be true. |
| `gpu_frac` | Must be 1. A lower value means the host shares each GPU between renters. |
| `verified` | Shown in the quote, and used by `--trust verified`. |
| Remaining rental duration | Must cover the startup estimate plus `--hours`. |

The GPU-only sticker price is shown in a separate column of the quote, and it is never used for ranking.

## Search rule

Vast returns at most 64 rows per query. The `truncated` field in the response was false on full pages in live checks, so the driver does not use it. The planner keeps the lowest total, and no unseen offer can cost less than the last row's all-in price times the hours plus the startup estimate, so the search stops when the lowest total is at or below that bound. Otherwise it reads more rows. If Vast rejects a later query, or after 10 pages, the search stops and labels the result "cheapest of the N offers checked".

Live checks showed two facts that change how the driver pages.

1. Ordering by `dph_total` orders by the all-in price, which includes the allocated disk.
2. A filter on `dph_total` compares the GPU-only price (`dph_base`), not the all-in price. A "price above the last row" filter on `dph_total` therefore skips rows whose GPU-only price is at or below the last all-in price while their all-in price is above it. With a disk of several hundred gigabytes this happens often, and the search would report a complete result that was missing offers.

The driver pages by `dph_base`, which the server both orders and filters on. It reads pages in order of GPU-only price and keeps them in a buffer. A row is handed to the search rule once its all-in price is below the GPU-only price of the last row read, because every row not yet read has a GPU-only price at or above that, and an all-in price at least as high. The search rule therefore sees pages ordered by the all-in price, as `ablit/select/search.py` expects. The next server query starts at the last GPU-only price, inclusive, and the driver drops rows it has already seen. When a server query returns rows below the price it was asked to start at, or when 64 or more rows share one GPU-only price, the driver stops and the result is labeled incomplete.

## Renting and cleanup

1. The run ID goes into the instance label. If a create request times out or its response is lost, the driver lists the user's instances and looks for the label before doing anything else, and it never sends a second create for the same run.
2. Every resource ID, including volumes, is recorded as soon as it is known.
3. Destroying a run must work even when new launches on this backend are disabled.
4. A paused instance still bills for its disk, so the driver treats pausing as not stopping billing.

## Watchdog credential

The watchdog on the rented machine needs to destroy its own instance. The driver creates an API key whose `api.instance.destroy` permission is limited to the one instance ID. It never puts the user's full account key on the machine. If the limited key cannot be created, step 3 cleans up and refuses to continue.

## Not yet verified

- Whether the limited destroy key works as described.
- Whether the advertised `inet_down` matches the measured speed after boot.
- How the server treats `type` values other than "on-demand". Interruptible offers are not searched yet.
- The `id` filter matched nothing in live checks, so `requote()` looks an offer up with `ask_contract_id`, which holds the same number and works.

## Step 3 API operations

The lifecycle implementation was checked against Vast's documentation on 2026-10-21. All HTTP mutation tests use invented responses. The launch path is untested on a rented machine. No account key or paid instance was used during implementation.

| Purpose | Request and documented response | Documentation |
|---|---|---|
| Search and requote | `POST /api/v0/bundles/` with the filters described above. Read the `offers` array. Requote uses `ask_contract_id` and the same allocated disk. | https://docs.vast.ai/api-reference/search/search-offers |
| Test credentials | One `GET /api/v1/instances?limit=1&select_cols=["id"]`. Require `success: true`. | https://docs.vast.ai/api-reference/instances/show-instances |
| Register the run's SSH public key | `POST /api/v0/ssh` with `ssh_key`. Require `success: true` and `key.id`. Registration happens before create, and Vast adds account keys to current instances too. | https://docs.vast.ai/api-reference/accounts/create-ssh-key |
| Create | `PUT /api/v0/asks/{offer_id}` with `image`, `disk` in GB, `label`, `runtype: "ssh"`, `target_state: "running"`, `cancel_unavail: true` and `onstart`. Require `success: true` and integer `new_contract`. No volume or secret environment is sent. | https://docs.vast.ai/api-reference/instances/create-instance |
| Reconcile, observe and confirm billing stop | `GET /api/v1/instances` with `limit: 25` and selected columns. Read `success`, `instances` and `next_token`. Follow every `after_token`. Space inventory reads at least 1.1 seconds apart to avoid bursts between reconciliation and verification. Match the run label or saved instance IDs locally. Read SSH connection fields, `host_id`, `actual_status` and the hourly `dph_total`. | https://docs.vast.ai/api-reference/instances/show-instances |
| Destroy | `DELETE /api/v0/instances/{instance_id}` with no body. Require `success: true`. A 404 is an idempotent destroy result, and a separate list check still has to confirm absence. The watchdog sends the same call with its scoped key. | https://docs.vast.ai/api-reference/instances/destroy-instance |
| Create the watchdog key | `POST /api/v0/auth/apikeys` with `name` and the permissions object below. Read integer `id` and non-empty `key`. | https://docs.vast.ai/api-reference/accounts/create-api-key and https://docs.vast.ai/cli/permissions |
| Revoke the watchdog key | `DELETE /api/v0/auth/apikeys/{key_id}`. Accept the documented JSON string `"Successfully Deleted API Key"`, or 404 on a retry. | https://docs.vast.ai/api-reference/accounts/delete-api-key |
| Remove the run's SSH public key | `DELETE /api/v0/ssh/{key_id}`. Require `success: true`. Failures stay pending for a retry. | https://docs.vast.ai/api-reference/accounts/delete-ssh-key |

The permission object contains only the destroy operation. `<instance_id>` below stands for the integer returned by create or recovered from the run label.

```text
{"api":{"instance_write":{"api.instance.destroy":{"constraints":{"id":{"eq":<instance_id>}}}}}}
```

The [permission mapping](https://docs.vast.ai/api-reference/permissions) places `api.instance.destroy` in `instance_write`. The [constraint examples](https://docs.vast.ai/cli/permissions) document the `id.eq` restriction. No read, create, billing or broad instance permission is requested for the watchdog.

## Image and SSH launch

The pinned tag is `ghcr.io/ggml-org/llama.cpp:server-cuda`. Anonymous GET requests to GHCR's token and manifest endpoints returned index digest `sha256:31b39eacce2bab5fde9286ec3980ed5548a3987644e94da0d0c2092f93313538` on 2026-10-21. The `linux/amd64` manifest digest is `sha256:c65f01343624f55848a82a6b653880e53399377a92b371975eb3303c1d5647e7`, which is the launch pin. The image config declares CUDA 12.8.1, so this profile sends `cuda_max_good >= 12.8` in its offer search. The image label records source commit `1d88cb05f1a257f90741538ca3aca6c1b015c6d3` and version `b11749`.

An anonymous Docker pull used an empty Docker configuration. Inspection ran with networking disabled. The image has Python 3.12, curl, bash, tar and sha256sum. It has neither sshd nor ss. `/app/llama-server --version` reported build 11749 at the matching commit. The measured directories contain 4,386,037,760 bytes. The compressed layers total 2,594,121,813 bytes. The 10 GB image allowance is a conservative disk and transfer estimate that includes room for Vast's launch files. It is a choice by ablit, not a measured download charge.

Vast's [SSH launch mode](https://docs.vast.ai/guides/instances/connect/overview) replaces the image entrypoint and sets up SSH. The [Docker environment guide](https://docs.vast.ai/guides/instances/docker-environment) describes the injected setup scripts. The Ubuntu image is compatible with typical SSH setup, but whether Vast provisions SSH successfully on this exact image still needs the paid check. Entrypoint mode would leave the image responsible for SSH, so it is not used. The `onstart` command only creates private bootstrap directories. ablit copies the watchdog, reviewed shell scripts and configuration over SSH, then starts the watchdog before downloading model weights. The image's existing Python interpreter runs it without extra packages. The engine health check reads `/proc/net/tcp` and `/proc/net/tcp6` instead of depending on ss.

Only `llama` is claimed in `supported_architectures`. The [architecture table at the matching source commit](https://github.com/ggml-org/llama.cpp/blob/1d88cb05f1a257f90741538ca3aca6c1b015c6d3/src/llama-arch.cpp) names it. Other supported architectures have not been copied into the lock.

OpenSSH uses a private identity and known-hosts file per run. First contact uses `StrictHostKeyChecking=accept-new`, and subsequent connections reject changed host keys. The first key is not authenticated by an independent Vast fingerprint. The CLI never uses `StrictHostKeyChecking=no`.

## Persistence and cleanup choices

The CLI saves create intent and flushes it before issuing the only allowed create request for a run. The label is `ablit-<run_id>`. Even when a response is lost, a new driver process reads `create_sent` and can only reconcile. `ABLIT_FAULT=drop-create-response` tests that path. A missing label after an ambiguous create stays pending because the docs give no visibility deadline. A documented HTTP rejection is recorded separately, so a rejected request with no resource can be cleaned up.

The run creates only an instance with local disk. It never requests a separate volume. Unexpected non-empty `volume_info` is a typed response error, since the volume schema has no documented fields needed to clean up an unexpected volume safely. Instance absence is the billing-stop evidence. Neither a pause nor a destroy acknowledgement alone counts.

The watchdog token is saved in the OS keyring and sent over encrypted SSH to a private file on the instance. The account key stays local. The endpoint key is random, contains 32 bytes, and stays in the keyring as `ablit/<run_id>`. It is sent to a remote launcher on stdin and passed to the engine through its environment. Run JSON and environment files contain no keys. Cleanup revokes the scoped key only after the instance is confirmed absent, and removes the run's account SSH key. Unknown credential responses keep cleanup pending.

The watchdog publishes its destroy decision ten seconds before calling the API, within its two-minute reserve. An attached CLI can save the heartbeat before the instance vanishes. Exit 8 requires saved watchdog cap evidence and confirmed instance absence. If no CLI collected the last heartbeat, disappearance alone does not prove why the instance ended. The code reports a bootstrap or unknown stop rather than inventing watchdog evidence.

The local cap check uses the larger of the locked and observed hourly rates, elapsed time, quoted transfer allowance and the remote heartbeat estimate. It runs during up, tunnel and status --watch when the heartbeat is more than three minutes old. The API does not document cumulative cost or received-byte units for the instance list, so the CLI labels its figure as an estimate. It never reports that estimate as a final bill.

## Documentation gaps and conservative handling

- The create operation schema specifies PUT, while its shared introduction says POST. The driver follows the operation schema and never retries with another method.
- API-key creation documents `permissions` only as a generic object. The CLI permissions page supplies the nested operation constraint syntax. The paid check must establish that the service enforces the constraint.
- The API-key list schema omits key names, key values and a way to recover a lost creation response by name. An ambiguous key creation is not retried. Cleanup asks for console review and stays at exit 7.
- SSH-key registration can also succeed before its response is lost. The code saves registration intent and does not repeat it. An unknown registration ID needs console review.
- The instance list has no documented cumulative USD charge or transfer counter, and there is no documented final-cost field on destroy. Final cost remains unknown. Ambiguous numeric cost fields are not guessed.
- The list schema gives examples for `dph_total` but little unit text. The hourly interpretation follows the [instance pricing guide](https://docs.vast.ai/guides/instances/pricing). No field is treated as a cumulative total. Unknown types raise typed errors.
- The REST search schema labels inet_down as MB/s, while the CLI search reference labels the same field as Mb/s. The driver follows the CLI field reference at https://docs.vast.ai/cli/reference/search-offers and BUILD.md, and refuses unknown value types. The docs also do not resolve every decimal versus binary bandwidth-unit convention. The existing search parser cross-checks the per-GB and per-TB fields at its tested ratio, and cost arithmetic follows BUILD.md's decimal GB rule. Transfer spend remains an estimate.
- The docs give no bound on create visibility, deletion visibility or key revocation. Empty results cannot clear an ambiguous create without a recovered instance ID. Pending work stays on disk for retries.
- SSH mode promises injected setup but gives no exact supported-package contract for this pinned CUDA image. Connection and bootstrap failure lead to cleanup, without another rental.
- No authenticated SSH host fingerprint field is documented. First contact uses trust on first use.
- No documented error discriminator identifies a workload-policy rejection separately from authentication, permission or invalid-argument errors. Unknown errors are refused, and the code does not retry elsewhere. Exit 9 remains reserved for an explicit workload rejection.
- SSH-key deletion documents an already-missing key as HTTP 400 with `error: no_ssh_key`. A successful prior deletion is recorded locally. An ambiguous delete followed by that exact documented error is treated as already removed. Other HTTP 400 responses stay pending.
