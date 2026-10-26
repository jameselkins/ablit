# Using ablit from an agent

This file is for coding agents and scripts that drive the `ablit` command. Contributors working on the code should read [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/architecture.md](docs/architecture.md). The [command reference](docs/cli-reference.md) lists every flag.

## What an agent can do alone

These commands spend nothing:

1. `ablit catalog list` prints every entry and its test status.
2. `ablit catalog show <id>` explains one entry: hardware floors, sizes, sources and notes.
3. `ablit plan <model> --json` searches the marketplaces and prints the chosen offer, the next four and their full cost.
4. `ablit status --all --json` prints every local run and its state.

`plan` reads public offer lists from Vast, RunPod, Clore and Lium, and needs no key.

## Renting

`ablit up` rents a machine and costs money. In a terminal it asks the user to type `rent`. An agent passes `--yes` to approve the quote without the prompt.

`ablit up` rents on Vast, RunPod, Clore and Lium. With no `--backends`, it searches Vast, and also RunPod, Clore and Lium when `RUNPOD_API_KEY`, `CLORE_API_KEY`, `LIUM_API_KEY` or a saved key for them exists, and it rents where the chosen offer is. Pass `--backends runpod` or `--backends clore` to rent on one only. RunPod renting is untested on a rented machine. It uses Secure Cloud offers only, because Community Cloud pods may have no public IP for SSH. `plan` shows the Community offers with that drop reason. When the chosen offer is on RunPod, the quote adds a note that the spend cap relies on the check that runs on this computer, so tell the user before they rent. Clore renting is untested on a rented machine. Clore has no scoped key, so when the chosen offer is on Clore the quote adds a note that the cap relies on the check on this computer and on the account balance, so tell the user before they rent. If the computer is off, nothing cancels a Clore order until the balance runs out. Lium renting is untested on a rented machine. A Lium key made with the user's key cannot be limited to one machine, so when the chosen offer is on Lium the quote adds a note that the cap relies on the check on this computer, and on Lium deleting the pod after a whole number of hours that ablit sets from the cap, so tell the user before they rent. `ablit setup runpod`, `ablit setup clore` and `ablit setup lium` save a key.

Every paid `up` needs `--max-spend <USD>`, and the watchdog destroys the run when spend reaches it. `--yes` skips only the prompt. ablit still refuses a cap too small to cover setup and ten minutes of use (exit 5), and it cancels if the price changes between the quote and the rental (exit 5). Add `--untested` for an entry whose status is `unverified`.

A typical flow:

1. Run `ablit plan <model> --json` and read `chosen`, `hourly_all_in_usd` and `totals`.
2. Start `ablit up <model> --max-spend <USD> --yes` in a managed process. It prints the endpoint block when the endpoint answers, and stays running to keep the SSH tunnel open and check spending. Do not wait for it to exit as a readiness signal.
3. Use `ablit status --json` from another process to follow the run. The endpoint is ready when the run has `state: "running"` and a `local_port`. Use `ablit logs` to read the logs.

Interrupting `up` closes its tunnel and stops its local spending check, and billing continues. Use `ablit tunnel <run>` to reconnect, or `ablit down <run>` to stop billing.

`ablit down [run]` destroys everything a run created and never asks to confirm. Run it whenever the user wants billing to stop, or a run is left over. Rerun it until it exits 0.

## Machine-readable output

`plan` and `status` accept `--json`. `up` prints text, so follow it with `ablit status --json`. Every JSON document has a `schema` field, now `1`. A change that breaks the shape raises that number. `catalog` and `logs` print text only.

Top-level keys of `plan --json`: `schema`, `command`, `checked_at`, `result`, `complete`, `incomplete_label`, `model`, `request`, `options`, `chosen`, `next`, `qualified_count`, `checked_count`, `filter_steps`, `backends`, `notices`. When `complete` is false, at least one marketplace did not answer, and the ranking may miss offers.

Messages and errors go to standard error as plain sentences.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Unexpected error while no run is billing |
| 2 | Usage error, including a paid `up` without a terminal |
| 3 | Model not supported, or no offer qualified |
| 4 | Missing or rejected credentials |
| 5 | Quote incomplete, or the cap can't cover setup and 10 minutes of use |
| 6 | Bootstrap, speed check or health check failed, the start deadline passed, SSH did not answer within five minutes of the machine running, or the run was stopped before the engine was ready |
| 7 | Cleanup pending, or billing stop not confirmed (including Ctrl-C before the endpoint is ready). Run `ablit down` again. |
| 8 | Cap reached, and the watchdog destroyed the run |
| 9 | The marketplace rejected the workload. ablit cleans up and doesn't retry elsewhere. |

## Secrets

The marketplace key lives in the OS keyring, or in `VAST_API_KEY` (Vast), `RUNPOD_API_KEY` (RunPod) or `CLORE_API_KEY` (Clore) or `LIUM_API_KEY` (Lium). ablit reads it, and an agent never needs to. Do not print, read or copy it.

`ablit status --show-key` prints the endpoint key for a run. Use it only when the user asks for the key.

## Spend cap

A watchdog on the rented machine destroys it when spend reaches the cap, and `ablit up` checks too while it runs. If the machine freezes or loses its network while the user's computer is off or asleep, neither check runs, and the marketplace keeps billing. Tell the user this before they rent. `ablit down` is the way to make sure billing has stopped.
