# RunPod driver notes

The driver is `cli/ablit/backends/runpod.py`. It searches with the GraphQL API and rents with REST API v2. **Renting is untested on a rented machine.** Nobody has rented a pod with it, so every rental call below follows RunPod's published schema and a synthetic test, and nothing more. The `rent` capability stays off until a paid round trip works.

The search was checked on 2026-10-20 with anonymous, read-only GraphQL queries. The rental facts were read on 2026-10-21 from RunPod's OpenAPI schema and documentation, and no write call was sent to RunPod. Anything the sources do not say is marked UNVERIFIED.

Sources used on this page:

- OpenAPI schema for REST API v2: https://api.runpod.io/v2/openapi.json (called "the schema" below)
- Migrating from v1: https://docs.runpod.io/api-reference-v2/migrate-from-v1
- Pod billing: https://docs.runpod.io/pods/pricing
- SSH: https://docs.runpod.io/pods/configuration/use-ssh
- Pod environment variables: https://docs.runpod.io/pods/templates/environment-variables
- API keys: https://docs.runpod.io/get-started/api-keys and https://docs.runpod.io/get-started/credentials
- Managing pods: https://docs.runpod.io/pods/manage-pods
- Pod overview and limitations: https://docs.runpod.io/pods/overview

## Which API rents

The driver uses REST API v2, with base URL `https://api.runpod.io` and paths under `/v2`. REST API v1 (`https://rest.runpod.io/v1`) is deprecated and RunPod says it will be retired on 2026-11-15 (https://docs.runpod.io/api-reference-v2/migrate-from-v1). The old plan to rent with the GraphQL mutation `podFindAndDeployOnDemand` is dropped, because v2 is the documented way forward.

The key goes in `Authorization: Bearer <key>`. The schema's security scheme says keys are limited to the permissions given when they were created, and that a valid key without access gets 403.

| Call | Operation in the schema | Used for |
|---|---|---|
| `GET /v2/pods?limit=1` | `listPods`, scope `pods:read` | The one read-only call in `ablit setup runpod` |
| `GET /v2/pods` | `listPods` | Finding a run's pod by name, watching it, and checking that billing stopped. It follows `pagination.nextCursor` until `hasNextPage` is false. |
| `POST /v2/pods` | `createPod`, scope `pods:write` | Renting one pod. Never sent twice for one run. |
| `DELETE /v2/pods/{id}` | `deletePod`, scope `pods:write` | Terminating a pod. 204 means deleted. 404 counts as already gone. |

Rate limits are per minute, hour and day, and a 429 answer carries `Retry-After` (https://docs.runpod.io/api-reference-v2/overview and the schema). The driver retries reads and deletes on 429 and 5xx. It never retries a create.

## Search

RunPod uses GraphQL for search. The driver asks for GPU types with their lowest price for the requested arrangement.

```
gpuTypes {
  id displayName memoryInGb
  lowestPrice(input: {gpuCount: <n>, minMemoryInGb: <n>}) {
    uninterruptablePrice
    stockStatus
    minMemory
  }
}
```

The endpoint is `https://api.runpod.io/graphql`. Anonymous queries work, and the driver sends `Authorization: Bearer` only when `RUNPOD_API_KEY` is set. The server does not allow introspection, so the field names come from live queries. The v2 catalog (`GET /v2/catalog/gpus`) answers 401 with no key, so search stays on GraphQL, which needs no key.

1. Leave `secureCloud` out of the input to see community prices as well. The driver sends a second query with `secureCloud: true` for Secure Cloud prices. `--trust verified` keeps only the Secure Cloud offers. When both queries give the same price for a GPU type, the driver keeps only the Secure Cloud offer.
2. `uninterruptablePrice` is the hourly total for all GPUs, so it is not multiplied by the GPU count again.
3. An offer counts as available only when `stockStatus` is not null. A GPU type with no price for the requested count and RAM is not an offer. `minMemory` is the RAM that comes with the price, and the planner uses it as the host RAM.
4. RunPod reports no download speed per host. The planner keeps these offers, labels them "speed not advertised, measured after boot", and ranks them as if they ran exactly at the speed floor. The run loop and the cap reserve use the same floor.
5. The hourly price used for ranking is `uninterruptablePrice` for all GPUs plus the disk, at $0.10 per GB per month divided by 730 hours (source: https://docs.runpod.io/pods/pricing). Transfer is counted as zero.
6. RunPod gives one price per GPU type and count, so the search is complete for what RunPod exposes.
7. RunPod reports no compute capability, so `cli/ablit/backends/runpod_gpus.py` maps each known GPU type to the value on NVIDIA's lists (https://developer.nvidia.com/cuda-gpus and https://developer.nvidia.com/cuda-legacy-gpus, read 2026-10-20). A type that is not in the table keeps no capability, and the planner's engine floor removes it with the reason "compute capability unknown" when the entry sets a minimum.

UNVERIFIED: whether the GraphQL `id` of every GPU type is accepted as `gpu.id` by `createPod`. The schema example is `NVIDIA GeForce RTX 4090`, which matches the GraphQL style (the 2026-10-21 anonymous list holds ids such as `NVIDIA A100 80GB PCIe`), but the schema lists no enum for `gpu.id`. A wrong id is a 400 or 422 answer, and the driver marks the create as refused.

UNVERIFIED: whether `minMemoryInGb` in the price query is per GPU or for the whole pod. The driver treats it as the pod total, as it did before. The quote check below compares the allocated RAM with the quoted RAM, so a mismatch ends the run before it is used.

## Renting

The create body the driver sends (schema `CreatePodRequest`, which rejects unknown fields):

| Field | Value | Source |
|---|---|---|
| `name` | `ablit-<run id>` | The only tag a create can carry. See "Lost create". |
| `image` | The pinned image with its digest. A tag with no digest is refused. | `BaseContainerConfig.image` in the schema. UNVERIFIED that RunPod pulls `image@sha256:...` by digest. |
| `gpu.id`, `gpu.count` | From the offer ID | `CreateGpuConfig`. The create places this one GPU type and never falls back to another. |
| `gpu.minRamPerGpu` | The offer's RAM divided by the GPU count, rounded down | The schema calls it a placement filter. |
| `cloud` | `SECURE` for a Secure Cloud offer, `COMMUNITY` for any other | `Cloud` in the schema. An offer from the any-cloud query that survived the duplicate rule is cheaper than Secure Cloud, so it is a community offer. |
| `disk` | The plan's allocated disk in GB | Container disk, "ephemeral, wiped on restart". |
| `mounts` | Left out, so there is no persistent volume | A stopped pod bills for its volume, and ablit never stops a pod. |
| `ports` | `["22/tcp"]` | The schema says `ssh.direct` needs `22/tcp` in `ports`. |
| `env` | `{"PUBLIC_KEY": "<run public key>"}` | https://docs.runpod.io/pods/configuration/use-ssh uses `$PUBLIC_KEY`. The run key is public, and the engine secrets travel over SSH. |
| `entrypoint`, `cmd` | `["/bin/bash", "-c"]` and the start script | `BaseContainerConfig` in the schema. |

The driver leaves out `startSsh`. That flag copies the account's registered keys into `PUBLIC_KEY`, and the only call that sets those keys (`PUT /v2/account/ssh-keys`) replaces the whole set, which would delete the user's own keys. The driver sets `PUBLIC_KEY` itself.

The start script writes the key to `/root/.ssh/authorized_keys`, installs `openssh-server` with apt when `/usr/sbin/sshd` is missing, generates host keys, and runs `sshd` in the foreground with password login off. RunPod's custom template steps (https://docs.runpod.io/pods/configuration/use-ssh) describe the same approach. UNVERIFIED on a rented pod: that the llama.cpp image and the Xolotl image can install sshd at boot. The Xolotl image holds no sshd today, because Vast adds one (`engines/xolotl/image/Dockerfile`). If apt cannot reach its mirrors on a pod, the run never gets SSH, and the bootstrap deadline destroys it.

No `terminateAfter` or `stopAfter` field exists in the v2 create schema, and the schema rejects unknown fields. The driver therefore has no marketplace deadline, `marketplace_deadline` stays off, and the quote says nothing about one.

After the create answers, the driver compares the pod with the approved quote: GPU type and count, allocated RAM (`gpu.memory`), container disk, cloud and hourly `cost`. A pod that differs ends the run, and `ablit up` destroys it. The cost may be up to 5% plus one cent above the quote before the pod is refused. UNVERIFIED: whether `cost` includes the disk, because the schema says only "Current cost in USD per hour". Only a clear overrun counts.

Provisioning is asynchronous. A pod starts as `PROVISIONING`, then `STARTING`, then `RUNNING` (the schema's `PodStatus`). A pod that is `RUNNING` with no direct SSH mapping yet counts as starting.

UNVERIFIED: whether `disk` accepts the size the plan asks for on every host, and whether a container disk with no volume is allowed on Community Cloud. A refusal comes back as 400 with a `detail` text, and the run ends with the create marked refused.

## SSH

ablit's tunnel needs real TCP SSH with port forwarding. The schema says the proxy (`ssh.proxy`, host `ssh.runpod.io`) "carries an interactive shell only", and that SCP, SFTP, rsync and port forwarding need `ssh.direct`. https://docs.runpod.io/pods/configuration/use-ssh says the same about SCP and SFTP for the proxy. The driver therefore uses only `ssh.direct`, the pod's public IP and the public port mapped to 22.

- The host and port come from `ssh.direct.host` and `ssh.direct.port` in the pod. The schema says `direct` is null until `22/tcp` is in `ports` and the running pod has a public port for it. `runtime.ports` lists the same mapping with an IP.
- The login is `root`, with the per-run ed25519 key that ablit makes. `ssh.direct.username` is `root` in the schema example.
- Secure Cloud pods always have a public IP, and the v1 create page says the `supportPublicIp` option applies to Community Cloud only (https://docs.runpod.io/api-reference/pods/POST/pods), which suggests Community Cloud pods may have none. v2 has no field for it. UNVERIFIED: whether a v2 community pod gets a public IP and a direct mapping. If it does not, `ssh.direct` stays null, the pod stays "starting", and the bootstrap deadline destroys it.

Host key situation: RunPod publishes no host key or fingerprint for a pod. The documentation says nothing about host key verification, and the schema's own `ssh.direct.command` text suggests adding `-o StrictHostKeyChecking=no` for short-lived pods. The sshd host keys are made by the start script at boot, so ablit cannot know them in advance. ablit therefore does what it does on Vast. OpenSSH saves the host key at first contact in a per-run `known_hosts` file (`StrictHostKeyChecking=accept-new`) and refuses a different key afterwards. First contact is trust on first use.

## Pricing and the quote

- GPU price: `uninterruptablePrice` for all GPUs, from the search.
- Disk: https://docs.runpod.io/pods/pricing says container disk costs $0.10 per GB per month while the pod runs and nothing while it is stopped, and volume disk costs $0.10 while running and $0.20 while stopped. The driver asks for container disk and no volume, so the disk term in the quote (GB times $0.10 divided by 730 hours) matches what the pod bills while it runs. UNVERIFIED: the number of hours RunPod counts in a month. The quote uses 730.
- Transfer: the pricing page says "There are no data ingress or egress fees", so transfer stays zero. The model download is charged as time only.
- Billing is per second, and the pricing page says Pods stop when the balance reaches $0. An on-demand deploy needs at least one hour of credits for the chosen configuration. The default spend limit is $80 per hour across all resources.
- RunPod charges no one-time order fee that the docs mention, so `fixed_fees` stays zero.

## Lost create

A create has no idempotency key and no tag field. The driver therefore:

1. Saves in the run record, before it sends anything, that a create was sent, with the start time.
2. Sends `POST /v2/pods` once. It never retries.
3. Names the pod `ablit-<run id>`. If the answer is lost, `up` or `down` lists the account's pods, follows every page, and takes the pod with that name that is not `TERMINATED`.
4. If the pod is not listed yet, raises `CreateUncertain`. The next `up` or `down` reads the list again and never sends a second create.

A refusal that proves nothing was created (HTTP 400, 401, 402, 403, 404 or 422) marks the create as refused. The schema says 400 covers both a rule violation and no capacity, and either way nothing was created. 429 and 5xx answers do not prove that, so they leave the create unmarked. Names need not be unique in RunPod, so two pods with the same name stop the driver from guessing, because a run expects one pod.

## Cleanup and proof that billing stopped

`ablit down` calls `DELETE /v2/pods/{id}` for each pod, which the schema calls irreversible and says releases compute and destroys the pod's host-local storage. It never calls the `stop` action, because https://docs.runpod.io/pods/manage-pods and the pricing page say a stopped pod keeps its volume and still bills for it. A pod that belongs to a Cluster answers 409, which ablit does not expect because it never creates one.

The proof is the pod list. A pod that is absent, or listed as `TERMINATED`, no longer bills. A pod listed in any other state, including `EXITED`, counts as still billing, and `down` exits with code 7. The v2 API shows no final cost, so ablit reports none. `GET /v2/billing/pods` exists, and the driver does not use it yet.

## The spend cap and scoped keys

What RunPod offers, from the sources above:

- Keys are created in the console, and the v2 schema has no call that creates or limits a key. The schema's only authentication scopes are per resource family (`pods:read`, `pods:write`, and others), so no scope covers one pod.
- The API key page describes the Restricted level only for Serverless endpoints, and says nothing about Pods or a single pod. UNVERIFIED: whether the console can make a key with `pods:write` only.
- RunPod sets a `RUNPOD_API_KEY` variable in every pod and calls it a "Pod-scoped API key". The documentation says nothing about what it may do. UNVERIFIED: whether it can delete its own pod, and whether it can touch any other pod.
- An SSH session does not inherit the container environment. The watchdog therefore also reads `/proc/1/environ`. UNVERIFIED that the variable is there.

So the fallback follows the Clore pattern:

1. `watchdog_credential` returns None, and the driver never puts the user's key on the machine. The `scoped_watchdog_credential` capability stays off.
2. The watchdog still counts spend on the box and writes its heartbeat. At the cap it tries `DELETE /v2/pods/<id>` with the pod's own key. That attempt is a bonus, and nothing relies on it.
3. The cap is the local check that `ablit up` runs while it is open. On a RunPod run, that check does not wait for the heartbeat to go stale, because a heartbeat that says "destroy" does not prove the pod is gone.
4. The quote and `ablit plan` add a note after the standard spend cap notice when the chosen offer is on RunPod. It says the cap relies on the check that runs on this computer, and that the pod's self-destroy attempt is untested.
5. If the user's computer is off or asleep, nothing ends the pod except the pod's own attempt and an empty balance. The user should keep only an amount they are willing to spend in the RunPod account. RunPod stops pods when the balance reaches $0 (pricing page).

## Memory lock limit for Xolotl

Question: the Xolotl bootstrap needs `ulimit -l` of at least 62914560 KB, and Vast containers had 8192 KB. Can a RunPod pod raise the limit?

Finding: no RunPod page says. The sources show no way to ask for it.

- The v2 create schema (`CreatePodRequest`, `ContainerConfig`) has no field for ulimits, capabilities, privileged mode or shared memory, and it rejects unknown fields. A search of the whole schema for "ulimit", "memlock", "privileged", "capabilit", "shm" and "ipc" finds nothing.
- The templates overview (https://docs.runpod.io/pods/templates/overview) lists only the image, disks, ports, environment variables and start commands.
- https://docs.runpod.io/pods/overview says "Runpod runs Docker for you, so you cannot spin up your own Docker instance or use Docker Compose on Pods."
- The documentation index has no page on memory limits or ulimits.

Inside a pod the container runs as root (SSH is `root@...`), and root can raise a soft limit only up to the hard limit that the container started with. Whether that hard limit is unlimited depends on the host's Docker settings, which RunPod does not document. The answer is UNVERIFIED and has to come from one measurement on a real pod, with `ulimit -l`, `ulimit -Hl` and `cat /proc/self/limits`, on Secure Cloud and on Community Cloud, or from RunPod support. The Xolotl check is unchanged.

## Not yet verified

- That `createPod` accepts the GraphQL GPU type IDs, the image digest form, a container disk with no volume, and the `entrypoint` and `cmd` fields as the driver sends them.
- That the start script brings up sshd on the llama.cpp and Xolotl images, and that Community Cloud pods get a public SSH port.
- That `cost` in the pod includes the disk, and what RunPod counts as a month.
- Whether `minMemoryInGb` is per GPU or for the pod.
- Whether the pod's own `RUNPOD_API_KEY` can delete the pod, and whether the console can issue a key limited to `pods:write`.
- The memory lock limit inside a pod.
- The download speed that RunPod hosts deliver. The bootstrap speed check measures it after boot.
- How long GraphQL search will keep working anonymously. The v2 catalog needs a key.
