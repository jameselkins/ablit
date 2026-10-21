# Using ablit from an agent

This file is for coding agents and scripts that drive the `ablit` command. Contributors working on the code should read CONTRIBUTING.md and BUILD.md.

## What an agent can do alone

These commands spend nothing:

1. `ablit catalog list` prints every entry and its test status.
2. `ablit catalog show <id>` explains one entry: hardware floors, sizes, sources and notes.
3. `ablit plan <model> --json` searches the marketplaces and prints the chosen offer, the next four and their full cost.
4. `ablit status --all --json` prints every local run and its state.

`plan` reads public offer lists from Vast, RunPod and Clore, and needs no key.

## Renting

`ablit up` rents a machine and costs money. In a terminal it asks the user to type `rent`. An agent passes `--yes` to approve the quote without the prompt.

`ablit up` rents on Vast only. The Clore driver searches and has rental calls, but they are untested on a rented machine, and `up` does not use them yet. `ablit setup clore` saves a Clore key for when it does.

Every paid `up` needs `--max-spend <USD>`, and the watchdog destroys the run when spend reaches it. `--yes` skips only the prompt. ablit still refuses a cap too small to cover setup and ten minutes of use (exit 5), and it cancels if the price changes between the quote and the rental (exit 5). Add `--untested` for an entry whose status is `unverified`.

A typical flow:

1. Run `ablit plan <model> --json` and read `chosen`, `hourly_all_in_usd` and `totals`.
2. Run `ablit up <model> --max-spend <USD> --yes`. It prints text, ending with the endpoint block, and exits 0 when the endpoint answers.
3. Use `ablit status --json` and `ablit logs` to follow the run.

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
| 6 | Bootstrap, speed check or health check failed |
| 7 | Cleanup pending, or billing stop not confirmed. Run `ablit down` again. |
| 8 | Cap reached, and the watchdog destroyed the run |
| 9 | The marketplace rejected the workload. ablit cleans up and doesn't retry elsewhere. |

## Secrets

The marketplace key lives in the OS keyring, or in `VAST_API_KEY` (Vast), `RUNPOD_API_KEY` (RunPod) or `CLORE_API_KEY` (Clore). ablit reads it, and an agent never needs to. Do not print, read or copy it.

`ablit status --show-key` prints the endpoint key for a run. Use it only when the user asks for the key.

## Spend cap

A watchdog on the rented machine destroys it when spend reaches the cap, and `ablit up` checks too while it runs. If the machine freezes or loses its network while the user's computer is off or asleep, neither check runs, and the marketplace keeps billing. Tell the user this before they rent. `ablit down` is the way to make sure billing has stopped.
