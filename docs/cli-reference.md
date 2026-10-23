# Command reference

This page lists every `ablit` command, its flags, the quote that `ablit up` shows, and the exit codes. For a short introduction, read the README. For using the commands from a script or an agent, read AGENTS.md. For how the commands work inside, read [architecture.md](architecture.md).

## Commands

| Command | Behavior |
|---|---|
| `ablit setup [backend]` | First-run account flow. Keys go to the OS keyring, or are kept for the session only. ablit never writes a plaintext key without asking. |
| `ablit plan <model>` | Resolves the model to one catalog entry, filters and ranks offers from every enabled backend, and prints the chosen offer, the next four, and why other backends or offers were dropped. Rents nothing. |
| `ablit up <model>` | Runs `plan`, shows the quote, and asks the user to type `rent`. It then re-queries the offer, writes a local intent record, rents, starts the watchdog, bootstraps the engine, checks health, and prints the endpoint. It rents on Vast, RunPod, Clore and Lium. With no `--backends` it searches Vast, and also RunPod, Clore and Lium when a key for them exists, and rents where the chosen offer is. With no Vast key it searches only the backends that have a key. |
| `ablit status [run]` | Run state, resource IDs, time of the last marketplace check, spend against the cap, bootstrap stage, and pending cleanup. Supports `--all`, `--json`, `--watch` and `--show-key`. |
| `ablit logs [run] [--stage bootstrap\|engine\|watchdog] [-f]` | Streams logs from the machine over SSH, with secrets removed. |
| `ablit tunnel [run]` | Re-opens the SSH tunnel to a running endpoint. |
| `ablit down [run]` | Destroys every billable resource the run created and confirms with the marketplace. Never asks for confirmation. Works even for a backend whose new launches are disabled. |
| `ablit catalog list\|show <id>\|validate <file>` | Lists entries and their test status, explains one entry, or checks a contributor's file. |

`<model>` is a catalog ID or a Hugging Face repo ID. A repo ID that matches several entries, e.g. two quant versions, requires `--variant`. ablit never replaces one build with another. A repo that isn't in the catalog is refused with exit code 3 and a link to CONTRIBUTING.md.

## Flags for plan and up

| Flag | Default | Meaning |
|---|---|---|
| `--max-spend USD` | Required for `up`, with no default. May come from the config file, and the quote shows the resolved value. | The spend at which ablit destroys the run. It counts compute, disk, the model download and transfer charges. |
| `--hours N` | 1 | Session length used to rank offers. It doesn't stop the run. |
| `--spot` | off | Reserved for interruptible offers. The flag is refused until a spot round trip has been tested. |
| `--min-download MBPS` | the entry's value, or 500 | Floor on download speed |
| `--trust any\|verified` | `any` | `verified` keeps only Vast verified hosts and RunPod Secure Cloud. Clore has no verification, so `verified` drops every Clore offer. On Lium it keeps secure nodes and drops spot nodes. `any` shows the status without filtering. RunPod Community Cloud offers are dropped from every paid run, with the reason "Community Cloud pods may have no public IP for SSH". |
| `--min-reliability`, `--region`, `--max-price-hr` | none | Optional user filters |
| `--backends vast,runpod,clore,lium` | all enabled backends with keys | Limits the search. Output lists the backends left out and why. |
| `--variant ID` | none | Picks a build when a repo has several |
| `--endpoint tunnel\|public` | `tunnel` | A paid `up` accepts only `tunnel`. See "Endpoint" below. |
| `--untested` | off | Required to run `up` on an `unverified` entry |
| `--catalog PATH` | bundled catalog | Local catalog for testing. Paid launch from it also needs `--untested`. |
| `--json` | off | Versioned machine-readable output |

`up` also takes `--yes`, which approves the quote without the prompt so that an agent or a script can rent, `--another`, which allows a second active run of the same model, and `--report`, which is accepted and recorded in the run file. `--report` does not write a log yet, so a boot test record is written by hand from the output of `ablit logs`.

A paid `up` asks the user to type `rent`. With `--yes`, the required `--max-spend`, the cap check and the price recheck still apply. A paid `up` with neither a terminal nor `--yes` exits with code 2.

## Quote

The quote shows these items:

1. Offer ID, marketplace, GPU model and count, VRAM, host RAM, allocated disk, advertised download speed, verification status and remaining rental duration.
2. Model build and pinned revision, engine and pinned image digest, and the entry's test status.
3. All-in hourly price, the GPU-only sticker price in a separate column, the per-GB transfer price, and one-time costs.
4. Totals for 1 hour, 24 hours, 7 days and `--hours`, each including download charges and the hours spent downloading and starting.
5. The cap, the time at which spend would reach it, and the spend cap notice.

Any change to the price, offer, build or cap cancels the confirmation. If the offer is gone at re-query, ablit plans again and asks again.

The spend cap notice is printed on every quote, in the README and in `ablit up --help`. A test checks that this page and the README hold the exact text.

```
How the spend cap works: a watchdog on the rented machine destroys it when
spend reaches $<cap>, and ablit checks too while it is running on this computer.
If the rented machine freezes or loses its network while this computer is off
or asleep, neither check can run, and the marketplace keeps billing until you
run `ablit down` or the rental ends. Check `ablit status` if you leave a run
unattended.
```

When a marketplace deadline has passed its own test, the notice adds "<Marketplace> will also end this rental at <time>, even if both checks fail." Until then the notice doesn't mention it.

## Endpoint

The default is `--endpoint tunnel`. The engine listens on `127.0.0.1` on the machine, and ablit opens an SSH port forward that checks the host key and picks a free local port. When the endpoint answers, `ablit up` prints the following.

```
Endpoint: http://127.0.0.1:<local port>/v1   (through an SSH tunnel to the rented machine)
API key:  saved in your keyring as ablit/<run id>. Run `ablit status <run> --show-key` to print it.
curl http://127.0.0.1:<local port>/v1/chat/completions -H "Authorization: Bearer $ABLIT_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"<served name>","messages":[{"role":"user","content":"Hello"}],"max_tokens":8}'
Closing this tunnel doesn't stop billing. Run `ablit down <run>` to stop it.
```

The `public` value would open the marketplace's mapped port over plain HTTP. It is not built yet, and a paid `up` refuses it.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Unexpected error while no run is billing, or a catalog file that failed validation |
| 2 | Usage error, including a paid `up` without a terminal |
| 3 | Model not supported, or no offer qualified |
| 4 | Missing or rejected credentials |
| 5 | Quote incomplete, or the cap can't cover setup and 10 minutes of use |
| 6 | Bootstrap, speed check or health check failed, the start deadline passed, SSH did not answer in time, or the run was stopped before the engine was ready |
| 7 | Cleanup pending, or billing stop not confirmed, including Ctrl-C before the endpoint exists |
| 8 | Cap reached, and the watchdog destroyed the run |
| 9 | The marketplace rejected the workload. ablit cleans up and doesn't retry elsewhere. |

The codes are defined in `cli/ablit/exitcodes.py`.
