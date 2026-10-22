# Clore notes

The Clore driver is `cli/ablit/backends/clore.py`. It can search the marketplace, and it has calls to rent, watch, cancel and check an order. **Renting is untested on a rented machine.** Nobody has made an order with it, so every rental call below follows Clore's documentation and a synthetic test, and nothing more. `ablit up --backends clore` calls this code, and Clore is in the default list when a Clore key exists. The driver's `rent` flag stays off until a paid test passes.

Every call below was read from Clore's own documentation. Where the documentation is silent, this page says so under "Gaps in the documentation", and the driver refuses what it cannot confirm.

## What makes Clore different

- Clore bills per minute, and the price is per day for the whole server. The driver divides by 24 for an hourly figure. Billing stops when the order is cancelled.
- The renter pays a share of Clore's base fee on top of the listed price. The base fee is 10% and is split between renter and host, so the renter pays 5% on demand. A holder of CLORE who uses Proof of Holding pays less. The driver quotes 5%, and `requote` takes the higher of 5% and the account's own figure.
- Clore adds a one-time $0.10 order fee for USD payment. Its order page says it is charged only when the order stays active for more than 10 minutes. The planner counts it in every total, which can only overstate the cost.
- A server is rented whole or by GPU. ablit rents whole servers only. Partial rentals share the machine's RAM and disk with other renters. A server counts as rentable only if `rented` is false and all its GPUs are free.
- The host sets how long a rental can last (`mrl`, in hours, 6 to 3000 on the marketplace). There is no way to ask for a shorter order. The driver reads `mrl` as the remaining hours.
- Clore has no host verification, so `--trust verified` drops every Clore offer.
- ablit prices the payment currency `USD-Blockchain` only (USDT or USDC held as a USD balance). It is the one currency whose price Clore documents as USD per day, so no exchange rate is needed. Servers that list no price in it are left out and counted in the search note. Paying in CLORE can be cheaper, and ablit does not use that yet.

## Calls

Base URL `https://api.clore.ai/v1`. The key goes in a header named `auth` (not `Authorization`). Clore allows 1 request per second per key, and the driver waits 1.1 seconds between requests. Every answer has a `code` field. Code 0 is success, 3 is a bad key, 5 is the rate limit, 2, 4 and 6 are refusals, and 1 is a database error. Code 6 carries a name in an `error` field.

| Call | Used for | Documentation |
|---|---|---|
| `GET /marketplace` | Search and requote. Returns every listed server. The documentation says it needs a key, but it answered with no key on 2026-10-20 and 2026-10-21, so `plan` works without one. If Clore starts to refuse keyless calls, the search reports a credential error for Clore and the other backends carry on. | https://docs.clore.ai/for-hosts/api |
| `GET /wallets` | The one read-only call in `ablit setup clore`. Needs a key. | https://docs.clore.ai/for-hosts/api |
| `GET /renter_fees` | Requote only. Reads the account's base fee and order fee for `USD-Blockchain`. | https://docs.clore.ai/for-hosts/api |
| `POST /create_order` | Rents one whole server. The body is `currency`, `image`, `renting_server`, `type` ("on-demand"), `ports` ({"22": "tcp"}), `ssh_key`, `autossh_entrypoint` (true), `command` and `required_price`. | https://docs.clore.ai/for-hosts/api |
| `GET /my_orders` | Finds a new order, reads state, spend, price, fee and the SSH port, and checks that billing stopped. | https://docs.clore.ai/for-hosts/api |
| `POST /cancel_order` | Ends an order. The body is `{"id": <order>}`. The answer `order_not_active` means it had already ended and counts as done. | https://docs.clore.ai/for-hosts/api and https://docs.clore.ai/dev/reference/error-codes |

Other pages the driver relies on:

- Fees, and the renter's 5%. https://docs.clore.ai/for-renters/fee-structure
- Per minute billing, the order fee, how long access takes, and how to connect. https://docs.clore.ai/for-renters/order-management
- Partial GPU rental, and why ablit avoids it. https://docs.clore.ai/for-renters/partial-gpu-rental
- Error codes and names. https://docs.clore.ai/dev/reference/error-codes
- Referral terms. https://docs.clore.ai/getting-started/deposit-withdrawal/referral-program

### How the driver reads a server

`price.on_demand["USD-Blockchain"]` is dollars per day for the whole server. The driver refuses a price outside Clore's own limit of 0.1 to 10,000 dollars per day, and refuses a server where the undocumented `price.usd.on_demand_usd` disagrees with it. `mrl` above 3000 is refused, as is a `reliability` above 1 and a `cuda_version` that is not `major.minor`. Mixed-GPU servers, servers with an unknown GPU vendor and servers with no USD price are left out and counted in the search note. A CUDA floor from the catalog is applied inside the driver, because Clore has no server-side filter.

Clore reports no compute capability, so `clore_gpus.py` holds a table from Clore's GPU names to NVIDIA's published values. A name that is missing stays unknown and a profile with a compute capability floor drops it.

## Creating an order, and finding it again

`create_order` has no label field and no idempotency key, and the reference documents only `{"code": 0}` as its answer. The driver therefore:

1. Saves in the run record, before it sends anything, that a create was sent, with the server ID, the image and the time.
2. Sends `create_order` once. It never retries a create.
3. Reads `my_orders` up to three times to find the new order. It looks for an order on the same server with the same image, a creation time not earlier than the saved time (minus 5 minutes for clock drift), and `spot` false.
4. If the order is not there, raises `CreateUncertain`. The next `up` or `down` run reads the order list again and never sends a second create.

If an order matches on server and time but its image text differs, the driver refuses to adopt it and says so. If two orders match, it refuses to choose. Once a run has a saved order ID, the ID alone is used.

A definite refusal (HTTP 400, 401, 403 or 404, or code 2, 3, 4 or 6) marks the create as rejected. Code 1, code 5 and server errors do not, because they do not prove that nothing was created.

## The spend cap on Clore, and where it is weak

On Vast, ablit creates an API key that can only destroy one instance and puts it on the machine, so the machine can end its own rental. Clore documents nothing like that. Its keys "grant full access to your account", at most three exist per account, they are made only in the web page, and no call creates one with limits. So:

- `watchdog_credential` returns None, and the driver never puts the user's key on the machine.
- The box cannot cancel its own order. The watchdog can stop the engine, but billing goes on until the order is cancelled.
- The cap is the local check that `ablit up` runs while it is open, plus the host's rental length limit, which can be up to 3000 hours (125 days) and is no cap at all in practice.
- If the user's computer is off or asleep, nothing cancels the order. Clore also ends an order when the account balance runs out, after a short grace time of about 5 minutes. A small balance is therefore the only hard stop. Keep only what you are willing to spend in the Clore account.
- `ablit down` cancels each order and then checks `my_orders`. Absence from the list, or `expired` true, is the evidence that billing stopped. The driver reports no final cost, because the order list does not say whether the order fee was charged.

## Gaps in the documentation

These are things the documentation does not say. The driver either refuses them, handles both readings, or leaves them for the first paid test.

1. `create_order` response. The reference gives only `{"code": 0}`, and the quick reference shows an `order_id`. The driver does not depend on it.
2. No label, tag or idempotency key on `create_order`, so a lost answer is recovered by matching.
3. `required_price` is documented as price protection for on-demand orders, but not in which unit. The driver sends the listed USD price per day, as it is in the marketplace.
4. Which registries `image` accepts, and whether an `image@sha256:...` digest is allowed. The documentation says "a valid image from Docker Hub". The engine images are pinned by digest, and this is the first thing a paid test must show.
5. How `command` behaves together with `autossh_entrypoint`.
6. Which `pub_cluster` entry is the host, and what `tcp_ports` entries mean. The driver reads `"22:10022"` as container port and public port, from the examples.
7. The marketplace answers without a key although the documentation says it needs one.
8. `price.usd`, `reliability`, `cuda_version`, `allowed_coins`, `gpu_array` and `specs.gpus` appear in live answers and are not documented.
9. Units of `gpuram`, `ram` and `disk`, whether `disk` is free or total, and the unit of `net.down`. The driver reads them as GB, GB, GB and Mbps.
10. Whether the fee page's "minimum rental 6 hours" conflicts with per minute billing and "terminate anytime" on the order page.
11. Whether the order fee in `my_orders` is charged when the order lasts under 10 minutes, and no final cost field.
12. `my_orders` has no status field and no label, and the documentation does not say whether finished orders are left out unless `return_completed` is set.
13. `mrl` is in hours in `marketplace` and in seconds in `my_orders`.
14. There is no call for one server, so a requote reads the whole marketplace.
15. No scoped keys, and no call to create a key.
16. The referral page does not say how long a referred account earns commission, or when the 50% promotion ends.
17. Which wallet name holds the USD balance, and exactly what code 5 means beyond the rate limit.
18. No bandwidth charge is documented, so the driver sets it to zero.
19. NVIDIA's compute capability list page is built by script, so `clore_gpus.py` names the pages it follows and notes that most entries were not re-read on 2026-10-21.

## Wiring into `ablit up`

- Backend choice. `--backends clore` is accepted. With no flag, Clore joins the search when `CLORE_API_KEY` or a saved key exists, as RunPod does. With no Vast key, only the backends that have a key are searched.
- Each run saves its `backend`, and `status`, `down`, `tunnel` and `logs` pick the driver from it.
- The quote adds the Clore note in `cli/ablit/notices.py`, and the run record sets `local_cap_primary`, so the local check ignores the heartbeat age.
- The 20 minute start deadline, exit 6 (ended with no endpoint, cleanup confirmed) and exit 7 (cleanup not confirmed) are shared with the other backends. `status` shows the saved record, without a call, when another command holds the run lock.
- A server that lacks a field ablit needs (a key that is missing or null) is left out of `plan` and counted in the search note as "servers missing a field ablit needs". A field with the wrong type still stops the search.
- The on-box watchdog has no Clore key and makes no call. It cannot cancel the order.

## SSH on Clore

Checked on 2026-10-22.

- Key login is documented. `create_order` takes `ssh_key` (up to 3072 characters, limited character set), and the user is `root`. https://docs.clore.ai/for-hosts/api and https://docs.clore.ai/for-renters/how-to-connect
- `ports` `{"22": "tcp"}` forwards the SSH port. `my_orders` returns `pub_cluster` (hosts) and `tcp_ports` (for example `22:10000`). https://docs.clore.ai/for-hosts/api
- `autossh_entrypoint` is described as Clore's entrypoint that deploys an SSH server and a custom `/root/onstart.sh`. https://docs.clore.ai/for-hosts/api
- Local port forwarding (`ssh -L`) is documented for rentals. https://docs.clore.ai/for-renters/how-to-connect
- Host key trust is ablit's own: first use is accepted and saved per run, and a change is refused. Clore documents no host key. The how-to-connect page mentions only removing an old key with `ssh-keygen -R` when a host key changes.
- UNVERIFIED: that the engine images (llama.cpp CUDA, Xolotl) run an SSH server under `autossh_entrypoint`, and that the key reaches `/root/.ssh/authorized_keys` with usable modes. The start script repairs modes for two minutes, as on Vast.
- UNVERIFIED: that the server allows TCP forwarding inside the container. The documentation shows `ssh -L` but not for an arbitrary image.
- UNVERIFIED: that the host in `pub_cluster` is the one that serves the forwarded port. The driver uses the first entry.
- UNVERIFIED: that a server lists a public SSH port at all. The marketplace documents no field about SSH or ports (https://docs.clore.ai/for-hosts/api), so `plan` cannot drop an unreachable server. A run that has no SSH port at the start deadline is cancelled, and its server goes on the local deny list so the next `plan` drops it.
- The Clore quick start page reads the SSH address from `order["connection"]["ssh"]` and uses `ssh_password`, not `ssh_key`. https://docs.clore.ai/dev/getting-started/quick-start That field is not in the API reference, and the driver does not use it. UNVERIFIED which of the two is current.

## Not yet done

- A paid round trip. Until one works, the `rent` capability stays off.
- Payment in CLORE, and spot orders.
