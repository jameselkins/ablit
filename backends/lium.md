# Lium notes

The Lium driver is `cli/ablit/backends/lium.py`. It can search the marketplace, and it has calls to rent, watch, delete and check a pod. **Renting is untested on a rented machine.** Nobody has rented a Lium pod with it, so every rental call below follows Lium's documentation and its OpenAPI schema, plus a synthetic test, and nothing more. `ablit up --backends lium` calls this code, and Lium is in the default list when a Lium key exists. The driver's `rent` flag stays off until a paid test passes.

Every call below was read from Lium's public documentation (https://docs.lium.io) and its OpenAPI schema (https://lium.io/api/openapi.json) on 2026-10-23. Where those pages are silent, this page says so under "Gaps in the documentation", and the driver refuses what it cannot confirm. The driver was written from those pages only. The Lium SDK source was not read, and no SDK code was copied. The SDK (https://github.com/Datura-ai/lium, PyPI package `lium.io`) is under the MIT license. The OpenAPI schema also names the MIT license.

## The claims that were checked

An earlier note said that Lium has a public offer list, per second billing, and API keys that can be scoped with spend caps. Each one was checked against Lium's own pages.

| Claim | Result | Source |
|---|---|---|
| A public offer list | Verified. `GET https://lium.io/api/public/v1/nodes` needs no key and no signup. A call on 2026-10-23 with no key answered HTTP 200 with 58 nodes. The page says it refreshes at most once a minute and allows 60 requests a minute per IP. | https://docs.lium.io/developers/public-nodes-feed |
| Per second billing | Verified. The charge is the hourly price times seconds divided by 3600, with no minimum. Charges are taken from the balance every 5 minutes, and once more when the pod is deleted. A pod is not charged while it starts, and once it is RUNNING it is charged from the deploy request. Charging stops at the delete request. | https://docs.lium.io/pod-users/billing |
| Keys with scopes | Verified. The scopes are `read`, `rent`, `manage` and `billing`. A key can also see only the pods it created (`pod_visibility: "own"`). | https://docs.lium.io/pod-users/api-keys |
| Keys with spend caps | Verified for a key that a person makes. A key can have a daily, monthly and lifetime budget in USD. A rent past a budget is refused with HTTP 402, and when spend reaches a budget the key's running pods are deleted. | https://docs.lium.io/pod-users/api-keys#budgets |
| Spend caps usable by ablit as a watchdog | Not usable. Keys are made and changed only from a signed-in session (`POST /keys` and `PATCH /keys/{id}` with a login token). The documentation says that a key made for an agent "cannot create other keys or raise its own budget", and that "managing API keys ... need a signed-in session. A key alone gets 401". ablit holds the user's API key and never a login session, so it cannot make a key for one run. | https://docs.lium.io/developers/claude-code-gpu and https://docs.lium.io/developers/api#authentication |

## What makes Lium different

- Lium is a marketplace on Bittensor Subnet 51. Providers list GPU nodes, and renters rent them by the second. https://docs.lium.io/intro
- Prices are US dollars per GPU per hour. A node rents by whole GPUs, from `min_rentable_gpu_count` up to the node's `gpu_count`. CPU, memory and disk are shared out in proportion to the GPUs taken. The driver builds one offer for the GPU count the request needs, and counts the nodes that cannot rent that way. https://docs.lium.io/developers/public-nodes-feed and https://docs.lium.io/pod-users/create-pod
- Lium names no renter fee and no order fee. The driver prices the listed price and sets the fees to zero. If the provider raises the price, a running pod moves to it only after an e-mail notice. The pod answer carries `pending_price`, and the driver uses the higher figure for the cap. https://docs.lium.io/pod-users/billing
- A node is `secure` or `spot`. A spot node may be reclaimed by its provider at any time, so the driver marks it interruptible, and `--spot` is needed to rent one. `--trust verified` keeps secure nodes. https://docs.lium.io/developers/public-nodes-feed
- Lium has no disk size field on a rent. The disk is the node's disk share for the GPUs taken, and about two thirds of it is the volume at `/root`. https://docs.lium.io/pod-users/create-pod
- Lium may not be used from US embargoed countries or by sanctioned parties. https://docs.lium.io/security/export-compliance
- Lium has a Refer and Earn program for renters (https://docs.lium.io/pod-users/refer-and-earn). ablit has no referral ID for Lium, so `ablit setup lium` shows no paid link.

## Calls

Base URL `https://lium.io/api`. The key goes in a header named `X-API-Key`, with no `Bearer` prefix. https://docs.lium.io/developers/authentication

| Call | Used for | Source |
|---|---|---|
| `GET /public/v1/nodes` | Search and requote. No key, and the driver sends none. | https://docs.lium.io/developers/public-nodes-feed |
| `GET /users/me` | The one read-only call in `ablit setup lium`. | https://docs.lium.io/developers/authentication (verify a key) |
| `POST /templates` | Makes a private one-time template for the run, with the image digest, port 22, the public key and the start command. | https://docs.lium.io/pod-users/templates and `CustomTemplateRequest` in https://lium.io/api/openapi.json |
| `POST /executors/{id}/rent` | Rents the node. The body is `pod_name`, `template_id`, `user_public_key`, `gpu_count` and `termination_hours`. The answer is `{"success": true, "pod_id": "<uuid>"}`. | https://docs.lium.io/developers/quickstart and `RentExecutorRequest` in the schema |
| `GET /pods` | Finds the run's pod by name, reads state, price, spend and the SSH command, and checks that billing stopped. The list has no pages. | `PodListItemResponse` in the schema |
| `DELETE /pods/{id}` | Deletes a pod. A 404 counts as done. | The schema, and https://docs.lium.io/developers/quickstart |
| `DELETE /templates/{id}` | Best effort cleanup of the template after a rent that Lium refused. A failure is ignored, because an unused template is free. | The schema |

Errors use one envelope, `{"success": false, "error": {"code", "message", "hint", "request_id"}}`, and the driver keeps `error.code`. 401 is a rejected key. https://docs.lium.io/developers/error-codes

### How the driver reads a node

`price_per_gpu_hour` times the GPU count is the price. Where `price_per_node_hour` is present, it must agree with the per GPU price within one cent, or the driver refuses the node. `reliability_score` is 0 to 100 and the driver converts it to 0 to 1, and refuses a value above 100. `tier` must be `secure` or `spot`. Only `id` and `rent_url` are always set in the feed, so a node that lacks a field ablit needs is left out and counted in the search note as "nodes missing a field ablit needs". A field with the wrong type or unit still stops the search. The node `id` is the offer's host ID for the local deny list.

Lium reports no compute capability, so `lium_gpus.py` holds a table from the GPU names seen in the feed on 2026-10-23 to NVIDIA's published values. A name that is missing stays unknown and a profile with a compute capability floor drops it. The Blackwell data center models B200 and B300 are left out until their values are read from NVIDIA's page.

## Creating a pod, and finding it again

The rent call takes the image only through a template, so a run makes two calls. Neither one is sent twice.

1. Saves in the run record, before it sends anything, that a create was sent, with the node ID, the pod name and the termination hours.
2. Creates the private one-time template named `ablit-<run>`. `docker_image` is the repository and `docker_image_digest` is the `sha256:` digest, so the image is pinned. If this step fails for any reason, no rent was sent, so the create counts as refused.
3. Sends the rent once. The pod name is `ablit-<run>`, which is the run label. The call also carries `Idempotency-Key: ablit-<run>`, which makes Lium answer a repeat of the call with the first pod. ablit never repeats a create anyway.
4. Reads `pod_id` from the answer. If the answer has another shape, the driver does not guess, and the pod is found by its name.

A lost answer is recovered by listing the pods and matching the name. A definite refusal (HTTP 400, 401, 402, 403, 404 or 422, or HTTP 409 with `node_paused_by_provider`, `provider_banned`, `node_below_bandwidth_floor`, `node_rent_in_progress`, `conflict` or `no_executor_matches_spec`) marks the create as rejected. HTTP 409 with `idempotency_in_progress` or `idempotency_outcome_unknown`, HTTP 429 and server errors do not, because they do not prove that nothing was rented.

The rent answer carries no price or hardware, so the driver cannot compare the created pod with the quote. The price is checked by the requote just before the rent, and the pod's own price is read by `observe`.

## The spend cap on Lium, and where it is weak

On Vast, ablit makes an API key that can only destroy one instance and puts it on the machine. Lium cannot do that:

- `watchdog_credential` returns None, and the driver never puts the user's key on the machine. The box cannot delete its own pod. The watchdog can stop the engine, but billing goes on until the pod is deleted.
- The cap relies on the check that `ablit up` runs while it is open on this computer. The run record sets `local_cap_primary`, so that check ignores the heartbeat age.
- Lium also deletes the pod itself. The driver sends `termination_hours`, which Lium documents as 1 to 720 hours. It is the cap divided by the hourly price, rounded down, and never below 1. This works while this computer is off. It counts whole hours, so a cap below one hour of rent still gets one hour of rent, and the local check ends the run sooner. It has not been tested on a rented pod, and `marketplace_deadline` stays False. https://lium.io/api/openapi.json (`RentExecutorRequest`) and https://docs.lium.io/pod-users/scheduled-termination
- A user who wants a hard limit can make a key with a daily budget on the Lium site and give that key to ablit. Lium then deletes the pods of a key that reaches its budget. ablit cannot read the key's budget, so the quote does not count on it.
- `ablit down` deletes each pod and then reads `GET /pods`. A pod that is absent, or listed as `CREATION_FAILED`, `BROKEN` or `DELETING`, is not billing, because the billing page says those are not charged. The driver reports no final cost.

## Gaps in the documentation

These are things the documentation does not say. The driver either refuses them, handles both readings, or leaves them for the first paid test.

1. Whether the node `id` in the public feed is the same ID that the rent path takes. The feed calls it a stable node ID and the quickstart takes an `executor_uuid` from `GET /executors`. Both are UUIDs, and the driver assumes they match.
2. How a digest in a template is read. The schema has `docker_image_digest`, and the run record page says a template that pins a digest shows `@sha256:...`. The driver sends `sha256:<hex>` with no tag. Whether the tag may be left out is not documented.
3. How Lium puts the key in `user_public_key` into a custom image, and whether the engine images (llama.cpp CUDA, Xolotl) run an SSH server. The template page says to include port 22 and a start command that keeps the container running. The start script appends the key from `PUBLIC_KEY`, installs sshd if it is missing and starts it unless port 22 is already in use. It is UNVERIFIED on a rented pod.
4. What `skip_add_agent_ssh_key` means. ablit leaves it unset, so Lium's default applies.
5. Which key scope can create and delete templates. The key page says `manage` covers changing or deleting templates, so a key with only `read` and `rent` may be refused at the template step. A refusal there ends the run with no pod. The default key holds `read`, `rent` and `manage`.
6. Whether `price` on a pod is the hourly price of the whole pod. The billing page says the pod price is the per GPU price times the GPUs, and `pending_price` is described as hourly USD.
7. Whether a deleted pod leaves the list at once. The billing evidence treats absence, and the three statuses above, as stopped.
8. The format of `ssh_connect_cmd`. The SDK reference describes it as `ssh <user>@<host> [-p <port>]`, and the driver accepts only that form with the user `root`. https://docs.lium.io/developers/sdk/reference
9. That a one-time template is deleted with its pod. The SDK reference says so for `one_time_template`.
10. Which scope `GET /users/me` needs. The docs use it to verify a key.
11. The compute capability table was not re-read from NVIDIA's page. NVIDIA's list is built by script.
12. Rate limits for the authenticated calls. The driver waits and retries reads and deletes on 429 and 5xx, honoring `Retry-After`, and never repeats a create. Template creation is limited to 5 per 5 seconds, 30 a minute and 300 an hour for private templates.
13. No bandwidth charge is documented, so the driver sets it to zero.
14. Whether `GET /pods` shows a pod with an unknown status. The driver refuses a status it does not know, for the run's own pods only.
15. Volume and backup charges exist, but ablit makes neither.

## Wiring into `ablit up`

- Backend choice. `--backends lium` is accepted. With no flag, Lium joins the search when `LIUM_API_KEY` or a saved key exists, as RunPod and Clore do. With no Vast key, only the backends that have a key are searched. `plan` searches Lium with no key.
- `ablit setup lium` reads the key with hidden input, tests it with `GET /users/me`, and can save it in the OS keyring. `LIUM_API_KEY` wins over the keyring.
- Each run saves its `backend`, and `status`, `down`, `tunnel` and `logs` pick the driver from it.
- The quote adds the Lium note in `cli/ablit/notices.py`.
- The 20 minute start deadline, exit 6 and exit 7 are shared with the other backends. A Lium run that has no SSH command at the start deadline puts its node on the local deny list.
- The on-box watchdog has no Lium key and makes no call. It cannot delete the pod.

## Not yet done

- A paid round trip. Until one works, the `rent` capability stays off.
- Volumes, backups and multi node clusters.
