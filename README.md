# ablit

ablit is a command-line tool, still being built, for people who want to run a language model on a rented GPU machine without comparing marketplaces and engine settings by hand. Given a model from its catalog, the finished tool will find the cheapest rented machine that meets the model's needs, show the full cost, and ask you to confirm. After you confirm, it will rent the machine, start the inference engine, download the model files, check that the engine answers, and print an OpenAI-compatible endpoint.

Project status: alpha. One catalog entry, `llama31-8b-abliterated-q4km`, has completed paid round trips on Vast. A round trip rented a machine, served chat completions through an SSH tunnel, and stopped billing with `ablit down`. In separate paid tests, the watchdog on the machine destroyed a run at its cap, and ablit recovered a run whose create response was lost. Every other entry is `unverified`, and RunPod can be searched but not rented yet.

## What ablit does

ablit runs on your computer with your own marketplace accounts. The planning code reads how much GPU memory, host RAM, disk and download speed a catalog entry needs, and it filters a list of machine offers against those needs. It then ranks the machines that remain by the total cost of the session, including the time spent downloading and starting. Both marketplace drivers can search for offers, and the Vast driver can rent the 8B entry. A paid launch asks you to type `rent` to confirm the quote. Agents and scripts can pass `--yes` instead, and the spend cap still applies.

## What ablit does not do

- It runs no service, holds no money, and relays no inference traffic.
- It never chooses a machine because of a referral commission. POLICY.md states the rule and names the tests that enforce it.
- It does not guarantee your spend cap. The notice below explains when the cap can fail.
- It does not run models that are missing from the catalog. A request for one exits with code 3 and a link to CONTRIBUTING.md.

## Install

ablit needs Python 3.11 or newer and OpenSSH.

```
uv tool install ablit
```

`pipx install ablit` works too. To run it from a checkout of this repository instead:

```
uv tool install --editable .
```

## Setup

Run `ablit setup vast` to enter a key with hidden input and test it with one read-only request. Choose the OS keyring to use the key in later commands. The session-only choice keeps the key only in the current process, which ends when setup exits. For a shell session, set `VAST_API_KEY` yourself with hidden input. An environment key takes priority over the keyring.

The setup flow asks whether you have an account. Without a Vast account, it shows the disclosure below and offers "Sign up with the paid link", "Sign up directly" and "Skip". It prints links and never opens a browser. RunPod account setup is deferred.

## Paid links

ablit has a referral ID for Vast.ai and none for RunPod. A user with no Vast account sees the disclosure and a choice between the paid link and a plain link. For a marketplace without an ID, only the plain link and the skip choice appear.

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

The cap counts compute, disk, the model download and transfer charges. If the cap cannot cover the download, the startup estimate and 10 minutes of use, `ablit up` exits with code 5 and shows the smallest cap that would work, including the watchdog reserve. Quotes use a conservative 10 GB image allowance.

## The endpoint

By default, the engine on the rented machine listens on `127.0.0.1` only, and ablit opens an SSH port forward that checks the host key and picks a free local port. ablit then prints a local URL, the name of the keyring entry that holds the endpoint key, and a sample `curl` command. Closing the tunnel does not stop billing, so you run `ablit down` to stop the run. The engine key is a random 32-byte value that is passed to the engine through an environment variable.

Paid launches in step 3 accept only `--endpoint tunnel`. Public endpoints are deferred. OpenSSH saves the first host key for each run and checks it on subsequent connections. Vast does not document a separate authenticated host fingerprint, so first contact uses trust on first use.

## Commands

Agents and scripts should read AGENTS.md. It covers `--json` output, exit codes, and the handoff to a person before anything is rented.

| Command | What it does |
|---|---|
| `ablit setup [backend]` | Vast account setup with hidden input and OS keyring storage. |
| `ablit plan <model>` | Shows the chosen offer and the next four, the totals for 1 hour, 24 hours, 7 days and `--hours`, and why other offers or backends were dropped. Rents nothing. Exits with code 3 when no offer qualifies. |
| `ablit up <model>` | Plans, asks you to type `rent`, then rents the 8B entry on Vast and keeps its tunnel open. |
| `ablit status [run]` | Run state, resource IDs, estimated spend and cleanup. Supports --all, --json, --show-key and --watch. |
| `ablit logs [run]` | Logs from the machine with known secrets removed. Supports stage filtering and -f. |
| `ablit tunnel [run]` | Re-opens the SSH tunnel and runs the local cap check. |
| `ablit down [run]` | Destroys the instance, confirms its absence and revokes run credentials. Retry exit 7 until cleanup is confirmed. |
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
| `cli/` | The Python package and its synthetic tests. `pyproject.toml` at the top builds it. |
| `catalog/` | Schema, version, license and the seed entries. |
| `tools/` | `verify_entry.py` checks entries, and `build_catalog.py` compiles them. |
| `backends/` | Notes on the request shapes of each marketplace. |
| `engines/` | Bootstrap and health scripts for vLLM, llama.cpp and Xolotl. None has run on a rented machine yet. |

## License

The code is licensed under Apache-2.0, and the catalog data under CC0-1.0. See LICENSE, NOTICE and `catalog/LICENSE`.
