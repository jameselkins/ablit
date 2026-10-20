# Vast driver notes

These notes describe what the Vast driver will send and read. Nothing has been sent to Vast yet, and the driver in `cli/ablit/backends/vast.py` is a stub. Each detail below is a plan to confirm against a live response, and the driver must refuse a response shape or unit it does not recognize.

## Search

Endpoint: `POST https://console.vast.ai/api/v0/bundles/`

The request body is a JSON object of filters. It follows these conventions.

1. `gpu_name` spells model names with spaces, for example "RTX 4090".
2. `gpu_ram` and `cpu_ram` are in megabytes.
3. `disk_space` is in gigabytes.
4. `compute_cap` is the compute capability multiplied by 100, so 7.5 is sent as 750.
5. The body always includes `rentable: {eq: true}`, a `type` for on-demand or interruptible offers, and `allocated_storage` set to the disk the run needs. Together with `disk_space >= N`, this makes the quoted price include the disk.
6. The body orders results with `order: [["dph_total", "asc"]]`.

The driver sends the user's key when there is one.

## Reading offers

| Field | Used for |
|---|---|
| `dph_total` | The all-in hourly price at the needed `allocated_storage`. |
| `inet_down` | Advertised download speed per host. Checked against the speed floor. |
| `inet_down_cost` | Transfer price per GB. Counted in the total cost. |
| `rentable` | Must be true. |
| `verified` | Shown in the quote, and used by `--trust verified`. |
| Remaining rental duration | Must cover the startup estimate plus `--hours`. |

The GPU-only sticker price is shown in a separate column of the quote, and it is never used for ranking.

## Search rule

Vast returns at most 64 rows per query. The planner queries with all filters on the server, ordered by `dph_total` ascending, and keeps the lowest total. No unseen offer can cost less than the last row's `dph_total` times the hours plus the startup estimate, so the search stops when the lowest total is at or below that bound. Otherwise it queries again for offers with `dph_total` above the last row's price. If Vast rejects that filter, or after 10 pages, the search stops and labels the result "cheapest of the N offers checked".

## Renting and cleanup

1. The run ID goes into the instance label. If a create request times out or its response is lost, the driver lists the user's instances and looks for the label before doing anything else, and it never sends a second create for the same run.
2. Every resource ID, including volumes, is recorded as soon as it is known.
3. Destroying a run must work even when new launches on this backend are disabled.
4. A paused instance still bills for its disk, so the driver treats pausing as not stopping billing.

## Watchdog credential

The watchdog on the rented machine needs to destroy its own instance. The driver creates an API key whose `api.instance.destroy` permission is limited to the one instance ID. It never puts the user's full account key on the machine. If the limited key cannot be created, the watchdog stops only the engine, and the quote says that the cap on this backend relies on the local check.

## Not yet verified

- The filter operator names and the exact response fields.
- Whether the limited destroy key works as described.
- Whether the advertised `inet_down` matches the measured speed after boot.
