# Vast driver notes

These notes describe what the Vast driver sends and reads. The search code in `cli/ablit/backends/vast.py` was checked on 2026-10-20 with anonymous, read-only search requests, and the renting code is not written yet. The driver refuses a response shape or unit it does not recognize.

## Search

Endpoint: `POST https://console.vast.ai/api/v0/bundles/`

The request body is a JSON object of filters. It follows these conventions.

1. `gpu_arch` is the GPU vendor, for example "nvidia". The driver does not filter by GPU model, because a catalog entry names no model. The names Vast reports for a GPU, such as "RTX 4090", are shown in the quote.
2. `gpu_ram`, `gpu_total_ram` and `cpu_ram` are in megabytes. The driver converts a gigabyte floor to megabytes by multiplying by 1000, because a card sold as 24 GB reports 24564 MB and would fail a floor of 24 GB if the driver divided by 1024.
3. `disk_space` is in gigabytes.
4. `compute_cap` is the compute capability multiplied by 100, so 7.5 is sent as 750.
5. The body always includes `rentable: {eq: true}`, `type: "on-demand"`, `gpu_frac: {eq: 1}` (so hosts that share one GPU between renters are left out), and `allocated_storage` set to the disk the run needs. Together with `disk_space >= N`, this makes the quoted price include the disk. A live check showed that the price in each row covers exactly the allocated size.
6. `num_gpus`, `inet_down` and `duration` (the remaining rental time in seconds) carry the GPU count, the download floor and the startup estimate plus `--hours`.
7. The user's filters are sent too: `verified`, `reliability`, `geolocation` (with the two-letter country code) and an upper bound on `dph_base` for `--max-price-hr`.
8. The body orders results with `order: [["dph_base", "asc"]]`, and a later page adds `dph_base: {gte: <last GPU-only price>}`. See "Search rule" for the reason.

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

The watchdog on the rented machine needs to destroy its own instance. The driver creates an API key whose `api.instance.destroy` permission is limited to the one instance ID. It never puts the user's full account key on the machine. If the limited key cannot be created, the watchdog stops only the engine, and the quote says that the cap on this backend relies on the local check.

## Not yet verified

- Whether the limited destroy key works as described.
- Whether the advertised `inet_down` matches the measured speed after boot.
- How the server treats `type` values other than "on-demand". Interruptible offers are not searched yet.
- The `id` filter matched nothing in live checks, so `requote()` looks an offer up with `ask_contract_id`, which holds the same number and works.
