# ablit

ablit is a command-line tool that finds the cheapest rented GPU machine that can run a model, shows the full cost, and starts the machine on your own marketplace account.

It is for people who want to run an open-weight language model, such as an abliterated model (a model edited to remove its refusals), on a rented GPU without comparing marketplaces and engine settings by hand. ablit runs on your computer, so your marketplace key stays with you, and it runs no service, holds no money and relays no inference traffic.

Status: alpha. Two catalog entries are boot-tested on Vast: `llama31-8b-abliterated-q4km` on llama.cpp and `ds41-september-gsq-uncensored` on Xolotl. The other entries have been planned but never booted. RunPod can be searched and rented, and RunPod renting has not been tested on a rented machine. The Status section below lists exactly what has been tested.

## Quickstart

ablit needs Python 3.11 or newer and OpenSSH.

```
uv tool install ablit
ablit plan llama31-8b-abliterated-q4km
ablit setup vast
ablit up llama31-8b-abliterated-q4km --max-spend 2
ablit down
```

First, `ablit plan` is free and needs no account. It prints the cheapest machine that meets the model's needs and the next four, with the full cost for 1 hour, 24 hours and 7 days. Second, `ablit setup vast` takes your Vast API key with hidden input and tests it, and you can choose to save it in the OS keyring. Third, `ablit up` shows the quote, asks you to type `rent`, then rents the machine, starts the engine, and prints a local endpoint that you can use with any OpenAI-compatible client. Last, `ablit down` destroys the machine and confirms that billing has stopped.

`--max-spend 2` is a cap of 2 US dollars, and it is required. Entries marked `unverified` also need `--untested`. Read the spend cap notice below before you rent.

Example output from `ablit plan llama31-8b-abliterated-q4km` on 2026-10-21, cut to the first lines. Prices change from minute to minute.

```
Chosen offer
  GPU:         RTX 3060 x1, 12.3 GB VRAM each
  Host RAM:    31.5 GB
  Price:       $0.0775 per hour all-in (GPU only: $0.0733)
  Totals, including download charges and the hours spent downloading and starting:
    1 hour         $0.14
    24 hours       $1.92
    7 days         $13.08
```

## How the spend cap works

The following notice is printed on every quote, in this file, and in `ablit up --help`.

```
How the spend cap works: a watchdog on the rented machine destroys it when
spend reaches $<cap>, and ablit checks too while it is running on this computer.
If the rented machine freezes or loses its network while this computer is off
or asleep, neither check can run, and the marketplace keeps billing until you
run `ablit down` or the rental ends. Check `ablit status` if you leave a run
unattended.
```

The cap counts compute, disk, the model download and transfer charges. If the cap cannot cover the download, the startup estimate and 10 minutes of use, `ablit up` exits with code 5 and shows the smallest cap that would work, including the watchdog reserve. Quotes use a conservative 10 GB image allowance.

## What ablit does not do

- It never chooses a machine because of a referral commission. POLICY.md states the rule and names the tests that enforce it.
- It does not guarantee your spend cap. The notice above explains when the cap can fail.
- It does not run models that are missing from the catalog. A request for one exits with code 3 and a link to CONTRIBUTING.md.
- It has no telemetry.

## Status

All paid tests so far ran on Vast.

- Tested on a rented machine: the 8B llama.cpp entry rented a machine, served chat completions through an SSH tunnel, and stopped billing with `ablit down`.
- Tested on a rented machine: the September Xolotl entry did the same on a 98 GB GPU. It downloaded 372 GB at 4.1 to 4.8 Gbps, was ready 17 minutes after the rental started, and generated about 47 tokens per second. The 24 GB GPU floor is not tested yet.
- Tested in separate paid runs: the watchdog on the machine destroyed a run at its cap, and ablit recovered a run whose create response was lost.
- Not tested on a rented machine: vLLM, the 1017 Xolotl entry, spot (interruptible) machines, and public endpoints. `ablit up` refuses the vLLM entries.
- RunPod: search works, and renting was added but has not been tested on a rented machine. Clore: search works, and its renting code is untested and not connected to `ablit up`.
- Both tested entries are marked `boot-tested` in the catalog, with their records in `catalog/boot-tests/`.

## Install

```
uv tool install ablit
```

`pipx install ablit` works too. To run ablit from a checkout of this repository instead, run `uv tool install --editable .` in the checkout.

## Setup

Run `ablit setup vast` to enter a key with hidden input and test it with one read-only request. You can then save the key in the OS keyring for later commands, or keep it for the current process only, which ends when setup exits. For a shell session, you can set `VAST_API_KEY` yourself with hidden input, and an environment key takes priority over the keyring.

Setup asks whether you already have an account. If you don't, it shows the disclosure in the Paid links section and offers "Sign up with the paid link", "Sign up directly" and "Skip". It prints links and never opens a browser. `ablit setup runpod` works the same way for RunPod, with `RUNPOD_API_KEY` as the environment key. ablit has no referral ID for RunPod, so setup offers only the plain sign-up link and skip.

## The endpoint

By default, the engine on the rented machine listens on `127.0.0.1` only, and ablit opens an SSH port forward that checks the host key and picks a free local port. ablit then prints a local URL, the name of the keyring entry that holds the endpoint key, and a sample `curl` command. Closing the tunnel does not stop billing, so run `ablit down` to stop the run.

Paid launches accept only `--endpoint tunnel`, and public endpoints are deferred. The endpoint key is a random 32-byte value that ablit passes to the engine through an environment variable. OpenSSH saves the first host key for each run and checks it on later connections, and neither Vast nor RunPod documents a separate authenticated host fingerprint, so first contact uses trust on first use.

## Commands

| Command | What it does |
|---|---|
| `ablit setup [backend]` | Vast, RunPod or Clore key setup with hidden input and OS keyring storage. |
| `ablit plan <model>` | Shows the chosen offer and the next four, the totals for 1 hour, 24 hours, 7 days and `--hours`, and why other offers or backends were dropped. Rents nothing. Exits with code 3 when no offer qualifies. |
| `ablit up <model>` | Plans, asks you to type `rent` (or takes `--yes`), then rents a llama.cpp or Xolotl entry on Vast or RunPod (`--backends runpod` for RunPod only) and keeps its tunnel open. |
| `ablit status [run]` | Run state, resource IDs, estimated spend and cleanup. Supports `--all`, `--json`, `--show-key` and `--watch`. |
| `ablit logs [run]` | Logs from the machine with known secrets removed. Supports `--stage` and `-f`. |
| `ablit tunnel [run]` | Re-opens the SSH tunnel and runs the local cap check. |
| `ablit down [run]` | Destroys the instance, confirms it is gone, and revokes run credentials. Run it again if it exits with code 7. |
| `ablit catalog list`, `show <id>`, `validate <file>` | Lists entries, explains one, or checks a contributor's file. |

Agents and scripts should read AGENTS.md, which covers `--json` output, exit codes, and renting with `--yes`. BUILD.md is the full specification, including every flag.

## Runtimes

A runtime, or engine, is the server program that loads the model on the rented machine and answers requests. ablit knows three.

| Runtime | What it is | Used for | Tested on a rented machine |
|---|---|---|---|
| [llama.cpp](https://github.com/ggml-org/llama.cpp) | A widely used C++ server for GGUF model files, run from its official CUDA image pinned by digest | Small and mid-size GGUF models on one GPU | Yes. The 8B entry passed paid round trips on Vast on 2026-10-21. |
| [vLLM](https://github.com/vllm-project/vllm) | A widely used Python server for full-size models across several GPUs | The DeepSeek FP8 entry on 8 GPUs, and the 70B entry | No. Its image digest is not pinned yet, so ablit refuses to rent it. |
| [Xolotl](https://github.com/Indras-Mirror/Xolotl) | A research engine by one developer, built to run DeepSeek-V4.1-Flash on one 24 GB GPU with about 90 GB of RAM. It keeps the most used experts in GPU memory and the rest in RAM and on disk. Its quant files load only in Xolotl. | The DeepSeek quants that fit a single consumer-class GPU | Yes. The September entry booted on Vast on 2026-10-21, on a 98 GB GPU. A 24 GB GPU is not tested yet. |

Xolotl boot test result: on 2026-10-21 the September entry ran on one RTX PRO 6000 (98 GB) on Vast. It downloaded 372 GB in about 11 minutes, read the Engram tables in place, and answered at about 47 tokens per second end to end. The record is in `catalog/boot-tests/`.

`ablit plan` works for every entry, whatever its runtime. `ablit up` is built for llama.cpp and Xolotl entries on Vast and RunPod. Both tested entries ran on Vast, and nothing has run on RunPod yet.

## Catalog

The catalog is a folder of YAML files in `catalog/models/`, one per model build, checked by `catalog/schema/v1.json`. It is curated, so it keeps the best build for each runtime and hardware tier and not every build that exists. The catalog data is released under CC0-1.0, and each entry records the license of the model it describes, which the catalog license does not change.

There are five entries. Two are `boot-tested`, which means a real run is recorded in `catalog/boot-tests/`. The other three are `unverified`: they passed the checks and have no recorded boot test.

| Entry | Runtime | Hardware | Why it is here |
|---|---|---|---|
| `llama31-8b-abliterated-q4km` | llama.cpp | One small GPU | Cheap abliterated model. Boot-tested. |
| `ds41-september-gsq-uncensored` | Xolotl | One 24 GB GPU, 90 GB RAM | DeepSeek-V4.1-Flash on one GPU. The author's own tests give it the best agentic scores of the single-GPU builds. Boot-tested. |
| `ds41-1017-xq-v3-uncensored` | Xolotl | One 24 GB GPU, 90 GB RAM | The newer 1017 checkpoint on one GPU. The author reports it trails the September build on the hardest agentic tasks. |
| `ds41-1017-abl-fp8` | vLLM | 8 GPUs with 80 GB each | The abliterated 1017 model at full FP8 precision |
| `llama33-70b-fp8-dynamic` | vLLM | From its sources | Unmodified 70B model, kept as a cheaper first test of the vLLM runtime |

Quality claims in this table come from the model authors and have not been checked by ablit. CONTRIBUTING.md explains how to add an entry, and each entry carries its own notes and the source URL for every hardware figure.

## Paid links

ablit has a referral ID for Vast.ai and none for RunPod. A user with no Vast account sees a disclosure and a choice between the paid link and a plain link. For a marketplace without an ID, only the plain link and the skip choice appear.

The commission rates are the ones the programs published when they were last checked, and they apply only to new accounts.

| Marketplace | Commission the maintainer earns | Checked |
|---|---|---|
| Vast.ai | 3% of what the new user spends there for the life of the account, paid by Vast as credits | checked 2026-10-21 |
| RunPod | 3% of Pod spend and 5% of Serverless spend, paid in RunPod credits for 6 months, and the new account must sign up with Google sign-in | checked 2026-10-20 |

The rules for paid links are these.

1. Links appear only in `ablit setup`, only for a marketplace where you have no account, and only next to the disclosure.
2. The commission never changes your prices, and it never affects which machine ablit picks. The planner cannot import the referral code, and tests check that the ranking does not change when referral IDs are set, unset or shuffled.
3. You can turn all referral links off with `ablit setup --no-referral`, with `ABLIT_NO_REFERRAL=1`, or with `referral = false` in the config file.
4. ablit has no telemetry.

The maintainer checks the rates on the program pages before each release and updates the date in the table. POLICY.md has the full rule and the five tests that enforce it.

## More documentation

| File | Contents |
|---|---|
| AGENTS.md | Using ablit from an agent or script, including `--json` output and exit codes |
| BUILD.md | The full specification of commands, flags, planner, drivers and engine templates |
| CONTRIBUTING.md | How to add a catalog entry or record a boot test |
| POLICY.md | The referral commission rule and its tests |
| SECURITY.md | How to report a problem, and what ablit does with secrets |

The repository holds the Python package in `cli/`, the catalog in `catalog/`, entry tools in `tools/`, marketplace request notes in `backends/`, and the engine scripts in `engines/`.

## License

The code is licensed under Apache-2.0, and the catalog data under CC0-1.0. See LICENSE, NOTICE and `catalog/LICENSE`.
