# Using ablit from an agent

This file is for coding agents and scripts that drive the `ablit` command. Contributors working on the code should read [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/architecture.md](docs/architecture.md). The [command reference](docs/cli-reference.md) lists every flag.

## What an agent can do alone

These commands spend nothing:

1. `ablit catalog list` prints every entry and its test status.
2. `ablit catalog show <id>` explains one entry: hardware floors, sizes, sources and notes.
3. `ablit plan <model> --json` searches the marketplaces and prints the chosen offer, the next four and their full cost. Add `--gpu <name>` to keep only offers with that GPU model.
4. `ablit status --all --json` prints every local run and its state.

`plan` reads public offer lists from Vast, RunPod, Clore and Lium, and needs no key.

## Renting

`ablit up` rents a machine and costs money. In a terminal it asks the user to type `rent`. An agent passes `--yes` to approve the quote without the prompt.

`ablit up` rents on Vast, RunPod, Clore and Lium. With no `--backends`, it searches Vast, and also RunPod, Clore and Lium when `RUNPOD_API_KEY`, `CLORE_API_KEY`, `LIUM_API_KEY` or a saved key for them exists, and it rents where the chosen offer is. Pass `--backends runpod` or `--backends clore` to rent on one only. RunPod renting passed one paid round trip on Secure Cloud on 2026-10-22. It uses Secure Cloud offers only, because Community Cloud pods may have no public IP for SSH. `plan` shows the Community offers with that drop reason. When the chosen offer is on RunPod, the quote adds a note that the spend cap relies on the check that runs on this computer, so tell the user before they rent. Clore renting is untested on a rented machine. Clore has no scoped key, so when the chosen offer is on Clore the quote adds a note that the cap relies on the check on this computer and on the account balance, so tell the user before they rent. If the computer is off, nothing cancels a Clore order until the balance runs out. Lium renting passed one paid round trip on 2026-10-26. A Lium key made with the user's key cannot be limited to one machine, so when the chosen offer is on Lium the quote adds a note that the cap relies on the check on this computer, and on Lium deleting the pod after a whole number of hours that ablit sets from the cap, so tell the user before they rent. `ablit setup runpod`, `ablit setup clore` and `ablit setup lium` save a key.

Every paid `up` needs `--max-spend <USD>`, and the watchdog destroys the run when spend reaches it. `--yes` skips only the prompt. ablit still refuses a cap too small to cover setup and ten minutes of use (exit 5), and it cancels if the price changes between the quote and the rental (exit 5). Add `--untested` for an entry whose status is `unverified`.

A typical flow:

1. Run `ablit plan <model> --json` and read `chosen`, `hourly_all_in_usd` and `totals`. `qualified_gpu_models` lists the GPU models that passed every other filter.
2. To pay more for a faster GPU, or to avoid one that failed, run `plan` again with `--gpu <name>` or `--exclude-gpu <name>`. Both take several names, comma separated or repeated, and match the marketplaces' spellings, so `--gpu 5090` matches "RTX 5090" and "GeForce RTX 5090". If no offer matches, `plan` exits 3 and names the GPU models that did qualify.
3. Start `ablit up <model> --max-spend <USD> --yes` in a managed process, with the same `--gpu` and `--exclude-gpu` flags. `up` applies them before it quotes. It prints the endpoint block when the endpoint answers, and stays running to keep the SSH tunnel open and check spending. Do not wait for it to exit as a readiness signal.
4. Use `ablit status --json` from another process to follow the run. The endpoint is ready when the run has `state: "running"` and a `local_port`. Use `ablit logs` to read the logs.

Interrupting `up` closes its tunnel and stops its local spending check, and billing continues. Use `ablit tunnel <run>` to reconnect, or `ablit down <run>` to stop billing.

Without `--another`, `up` refuses to rent when the model already has an active run. It resumes that run instead only when the run is still `creating`, its marketplace is one this `up` searches, the cap and build match, and no other `up` process is still running for it. Resuming covers an `up` that exited before the engine was ready, e.g. after a lost create response or Ctrl-C. A resumed run is named on standard error with its run ID, marketplace and state. Two `up` calls that run at the same time never share a run. To rent the same model on several marketplaces at once, pass `--another` to each `up` after the first.

A paid `up` holds a lock while it quotes and rents. A second `up` waits up to 15 seconds for it, and then exits 2 and asks you to run it again once the first `up` prints its run ID.

`ablit down [run]` destroys everything a run created and never asks to confirm. Run it whenever the user wants billing to stop, or a run is left over. Rerun it until it exits 0.

## Machine-readable output

`plan` and `status` accept `--json`. `up` prints text, so follow it with `ablit status --json`. Every JSON document has a `schema` field, now `1`. A change that breaks the shape raises that number. `catalog` and `logs` print text only.

Top-level keys of `plan --json`: `schema`, `command`, `checked_at`, `result`, `complete`, `incomplete_label`, `model`, `request`, `options`, `chosen`, `next`, `qualified_count`, `checked_count`, `qualified_gpu_models`, `filter_steps`, `backends`, `notices`. When `complete` is false, at least one marketplace did not answer, and the ranking may miss offers.

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

## Using the endpoint with Claude Code

For an engine that answers the Anthropic Messages API, `ablit up` prints a block of `export` lines after the curl example. Today that means llama.cpp and Xolotl, and not vLLM. The lines set:

```
export ANTHROPIC_BASE_URL=http://127.0.0.1:<local port>
export ANTHROPIC_AUTH_TOKEN="$(ablit status <run> --key-only)"
export ANTHROPIC_MODEL=<served name>
```

They also set `ANTHROPIC_DEFAULT_HAIKU_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_OPUS_MODEL`, `ANTHROPIC_DEFAULT_FABLE_MODEL` and `CLAUDE_CODE_SUBAGENT_MODEL` to the served name, and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`. The base URL has no `/v1`, because Claude Code adds `/v1/messages` itself. Use the block as printed, so the key is read from the keyring and never shown.

Claude Code's system prompt and tools take about 15K to 25K tokens. An entry whose context limit is smaller will refuse Claude Code's requests, and the block says so when the entry serves fewer than 25,000 tokens. Check the context limit with `ablit catalog show <id>` before you rent for Claude Code.

## Secrets

The marketplace key lives in the OS keyring, or in `VAST_API_KEY` (Vast), `RUNPOD_API_KEY` (RunPod) or `CLORE_API_KEY` (Clore) or `LIUM_API_KEY` (Lium). ablit reads it, and an agent never needs to. Do not print, read or copy it.

`ablit status --show-key` prints the endpoint key for a run. Use it only when the user asks for the key. `ablit status <run> --key-only` prints the key alone, for a shell to capture with `$(...)`. It reads the keyring and contacts no marketplace. Use it inside a command, as the Claude Code block does, and do not print its output.

## Spend cap

A watchdog on the rented machine destroys it when spend reaches the cap, and `ablit up` checks too while it runs. If the machine freezes or loses its network while the user's computer is off or asleep, neither check runs, and the marketplace keeps billing. Tell the user this before they rent. `ablit down` is the way to make sure billing has stopped.
