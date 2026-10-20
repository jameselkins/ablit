# ablit

ablit is a command-line tool, still being built, for people who want to run a language model on a rented GPU machine without comparing marketplaces and engine settings by hand. Given a model from its catalog, the finished tool will find the cheapest rented machine that meets the model's needs, show the full cost, and ask you to confirm. After you confirm, it will rent the machine, start the inference engine, download the model files, check that the engine answers, and print an OpenAI-compatible endpoint.

Project status: planning complete, no command rents anything yet.

The catalog commands work today, and so do the offer filtering, cost model, search rule, watchdog logic and secret filter that the other commands will use. The commands that rent, monitor and destroy machines print "not implemented yet" and exit with code 2. Every hardware floor in the catalog is labeled as not yet tested, because nobody has booted any of the seed entries.

## What ablit does

ablit runs on your computer with your own marketplace accounts. The planning code reads how much GPU memory, host RAM, disk and download speed a catalog entry needs, and it filters a list of machine offers against those needs. It then ranks the machines that remain by the total cost of the session, including the time spent downloading and starting. The marketplace drivers that fetch real offers are not written yet. A paid launch will need a terminal, because you must type `rent` to confirm the quote, and there is no `--yes` flag.

## What ablit does not do

- It runs no service, holds no money, and relays no inference traffic.
- It never chooses a machine because of a referral commission. POLICY.md states the rule and names the tests that enforce it.
- It does not guarantee your spend cap. The notice below explains when the cap can fail.
- It does not run models that are missing from the catalog. A request for one exits with code 3 and a link to CONTRIBUTING.md.

## Install

ablit is not released yet, so there is no package to install. Once it is released, the plan is to install it with `uv tool install ablit` or `pipx install ablit`. It needs Python 3.11 or newer.

To try the catalog commands from a checkout of this repository:

```
cd cli
uv run --with-editable . ablit catalog list
uv run --with-editable . ablit catalog show ds41-1017-xq-v3-uncensored
```

## Setup

`ablit setup` is the first-run account flow. It will store marketplace keys in your operating system keyring, or keep them for the session only if you prefer, and it will never write a plaintext key without asking. For each marketplace, in alphabetical order, it asks whether you already have an account. If you do, you enter a key, ablit tests it with one read-only call, and you see no referral link. If you do not, you see the disclosure described in the next section and three choices of equal weight, which are to sign up with the paid link, to sign up directly, or to skip. ablit prints links and never opens a browser. The setup command is not written yet.

## Paid links

No referral IDs are configured yet, so ablit offers no paid link for any marketplace. When the project has an ID for a marketplace, a user who has no account there sees the disclosure and a choice between the paid link and a plain link. Without an ID, only the plain link and the skip choice appear.

The commission rates are the ones the programs published when they were last checked, and they apply only to new accounts.

| Marketplace | Commission the maintainer would earn | Checked |
|---|---|---|
| Vast.ai | 3% of what the new user spends there, paid by Vast | checked 2026-10-20 |
| RunPod | 3% of Pod spend and 5% of Serverless spend, paid in RunPod credits for 6 months, and the new account must sign up with Google sign-in | checked 2026-10-20 |

The rules for paid links are these.

1. Links appear only in `ablit setup`, only for a marketplace where you have no account, and only next to the disclosure.
2. The commission never changes your prices, and it never affects which machine ablit picks. The planner cannot import the referral code, and tests check that the ranking does not change when referral IDs are set, unset or shuffled.
3. You can turn all referral links off with `ablit setup --no-referral`, with `ABLIT_NO_REFERRAL=1`, or with `referral = false` in the config file.
4. ablit has no telemetry.

The maintainer checks the rates on the program pages before each release and updates the date in the table.

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

The cap counts compute, disk, the model download and transfer charges. If the cap cannot cover the download, the startup estimate and 10 minutes of use, `ablit up` exits with code 5 and shows the smallest cap that would work.

## The endpoint

By default, the engine on the rented machine listens on `127.0.0.1` only, and ablit opens an SSH port forward that checks the host key and picks a free local port. ablit then prints a local URL, the name of the keyring entry that holds the endpoint key, and a sample `curl` command. Closing the tunnel does not stop billing, so you run `ablit down` to stop the run. The engine key is a random 32-byte value that is passed to the engine through an environment variable.

With `--endpoint public`, ablit uses the port that the marketplace maps to the machine. Before you confirm, it prints "This endpoint uses plain HTTP. Your API key and your prompts cross the internet unencrypted." TLS on the public endpoint is not part of the first version.

## Commands

| Command | What it does |
|---|---|
| `ablit setup [backend]` | First-run account flow. Not written yet. |
| `ablit plan <model>` | Shows the chosen offer and the next four. Rents nothing. Not written yet. |
| `ablit up <model>` | Plans, asks you to type `rent`, then rents, bootstraps and prints the endpoint. Not written yet. |
| `ablit status [run]` | Run state, resource IDs, spend against the cap. Not written yet. |
| `ablit logs [run]` | Logs from the machine with secrets removed. Not written yet. |
| `ablit tunnel [run]` | Re-opens the SSH tunnel. Not written yet. |
| `ablit down [run]` | Destroys every billable resource of the run. Not written yet. |
| `ablit catalog list`, `show <id>`, `validate <file>` | Lists entries, explains one, or checks a contributor's file. Works today. |

BUILD.md is the full specification, including every flag and exit code.

## Catalog

The catalog is a folder of YAML files in `catalog/models/`, one per model build, checked by `catalog/schema/v1.json`. The catalog data is released under CC0-1.0. Each entry also records the license of the model it describes, and the catalog license does not change that license.

The first scaffold has seven entries, and each one has the status `unverified`, which means it passed the checks and nobody has booted it.

| Entry | Engine |
|---|---|
| `ds41-1017-abl-fp8` | vLLM |
| `ds41-september-gsq-uncensored` | Xolotl |
| `ds41-1017-xq-v2-uncensored` | Xolotl |
| `ds41-1017-xq-v3-uncensored` | Xolotl |
| `ds41-1017-gsq-agentic-strata` | Xolotl |
| `llama33-70b-fp8-dynamic` | vLLM |
| `llama31-8b-abliterated-q4km` | llama.cpp |

CONTRIBUTING.md explains how to add an entry. Each entry carries its own notes and the source URL for every hardware figure.

## Repository layout

| Path | Contents |
|---|---|
| `cli/` | The Python package skeleton and its tests. |
| `catalog/` | Schema, version, license and the seed entries. |
| `tools/` | `verify_entry.py` checks entries, and `build_catalog.py` compiles them. |
| `backends/` | Notes on the request shapes of each marketplace. |
| `engines/` | Bootstrap and health scripts for vLLM, llama.cpp and Xolotl. None has run on a rented machine yet. |

## License

The code is licensed under Apache-2.0, and the catalog data under CC0-1.0. See LICENSE, NOTICE and `catalog/LICENSE`.
