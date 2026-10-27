# ablit

ablit runs abliterated and uncensored language models on rented GPUs, using your own marketplace account. Abliterated models are edited to reduce refusals. ablit finds the cheapest offer that meets a model's requirements and gives you a local OpenAI-compatible API through an SSH tunnel.

The quote includes the model download, startup time, disk and transfer charges. Model revisions and engine images are pinned, and `ablit down` destroys the rental and confirms that billing has stopped.

Status: alpha. Three catalog entries have [boot test records](catalog/boot-tests/) that match their current build, profile, engine image and bootstrap script. Renting has been tested on Vast, RunPod Secure Cloud and Lium. Clore rentals remain untested.

## Quickstart

You need Python 3.11 or newer, OpenSSH and [uv](https://docs.astral.sh/uv/). Install ablit from [PyPI](https://pypi.org/project/ablit/):

```sh
uv tool install ablit
```

To work from a local checkout, use `uv tool install --editable .`.

First, preview a quote for the small Llama 3.1 8B model. Planning is free and needs no account.

```sh
ablit plan llama31-8b-abliterated-q4km --backends vast
```

The quote shows the chosen offer and the next four, including totals for 1 hour, 24 hours and 7 days. Omit `--backends vast` to compare all four marketplaces.

Next, read [how the spend cap works](#how-the-spend-cap-works), then enter your Vast API key and start the model:

```sh
ablit setup vast
ablit up llama31-8b-abliterated-q4km --backends vast --max-spend 2
```

Setup accepts the key with hidden input and can save it in your OS keyring. `up` shows a quote and asks you to type `rent` before spending money. The required `--max-spend 2` sets a $2 budget, including setup.

Keep `up` running. When the model is ready, it prints a local endpoint and a curl example, and it keeps the tunnel and local spending check open. Use another terminal to check the run or stop billing:

```sh
ablit status
ablit down
```

Closing `up` or pressing Ctrl-C closes the tunnel, and billing continues. Reconnect with `ablit tunnel`, or stop the rental with `ablit down`. If `down` exits with code 7, cleanup is pending; run it again until it exits 0.

## Choose a model

Start with the 8B entry to check the rental workflow. Nanbeige and DeepSeek have enough context for a coding agent's longer prompts.

| Catalog entry | GPU and host RAM floors | Download | Context |
|---|---|---|---|
| `llama31-8b-abliterated-q4km` | One 8 GB GPU, 16 GB RAM | 4.9 GB | 4,096 tokens |
| `nanbeige43-33b-uncensored-q4km` | One 32 GB GPU, 32 GB RAM | 20.1 GB | 32,768 tokens |
| `ds41-1017-xq-v4-uncensored` | One 24 GB GPU, 90 GB RAM | 371.7 GB | 131,072 tokens |

All three entries have boot tests, but only the machines in their records have been tested. The 8 GB Llama floor and the 24 GB DeepSeek floor remain untested. Nanbeige's license reserves hosted inference rights, so read the model license before serving other people.

On 2026-10-26, the 552B DeepSeek V4 entry ran on one 48 GB RTX PRO 5000 with 96 GB of RAM. It was ready in about 25 minutes, including the download, and the engine generated 28 to 36 tokens per second. The run's estimated cost was $0.74. The [test record](catalog/boot-tests/ds41-1017-xq-v4-uncensored-2026-10-26.md) includes prompt processing, caching and tool use results. Prices and speeds depend on the machine.

DeepSeek uses [Xolotl](https://github.com/Indras-Mirror/Xolotl), a research engine that keeps experts across GPU memory, RAM and disk. Llama and Nanbeige use [llama.cpp](https://github.com/ggml-org/llama.cpp). Both engines are configured for one request at a time.

Inspect a build's requirements, sources and license before renting:

```sh
ablit catalog list
ablit catalog show nanbeige43-33b-uncensored-q4km
```

The [full catalog](catalog/models/) also includes earlier DeepSeek builds and unverified Ornith entries. The September and V3 DeepSeek entries are `unverified` because their test records cover older engine images. An `unverified` entry needs `--untested` to launch. The vLLM entries can be planned, but launches are blocked until the engine image is pinned and tested.

Use `--gpu 5090` or `--exclude-gpu 5090` with both `plan` and `up` to control which GPU models qualify. The [command reference](docs/cli-reference.md) covers the other filters.

## Use it with Claude Code

When a llama.cpp or Xolotl endpoint is ready, `ablit up` prints the shell exports for Claude Code. Paste the block into another terminal, then run `claude`. The block sets the base URL and all model aliases, and reads the endpoint key from your keyring with `ablit status <run> --key-only`.

The 8B entry's 4,096-token context is too small for Claude Code's system prompt and tools, which take about 15,000 to 25,000 tokens. Use an entry with more context, such as Nanbeige or DeepSeek V4. Streaming and tool calls through `/v1/messages` passed direct API tests; a complete Claude Code session has not been tested.

DeepSeek's follow-up turns took 30 to 35 seconds to reread a 20,000-token prompt with thinking enabled. With thinking disabled in direct API requests, the engine reused the conversation and answered short follow-ups in 3 to 5 seconds. See the [test record](catalog/boot-tests/ds41-1017-xq-v4-uncensored-2026-10-26.md) for the settings and measurements.

## Marketplaces and spending checks

`plan` searches all four marketplaces without a key. By default, `up` searches the marketplaces where you have configured credentials. Use `--backends vast`, `--backends runpod`, `--backends clore` or `--backends lium` to choose one explicitly, and `ablit setup <backend>` to save its key.

| Marketplace | Rental status | How the spend cap is enforced |
|---|---|---|
| [Vast.ai](https://vast.ai) | Tested | A watchdog on the machine uses a key limited to that machine, and ablit also checks locally. |
| [RunPod](https://www.runpod.io) | Tested on Secure Cloud | The cap relies on the local check. Paid runs use Secure Cloud because Community Cloud may provide no public IP for SSH. |
| [Lium](https://lium.io) | Tested | The cap relies on the local check. A provider deletion deadline, set in whole hours from the cap, provides an additional limit and remains untested. |
| [Clore.ai](https://clore.ai) | Untested | The cap relies on the local check. If your computer is off, the order continues until the account balance runs out. |

The local check runs while `up`, `tunnel` or `status --watch` is attached. RunPod, Lium and Clore do not receive your account key on the rented machine. Their [backend notes](backends/) explain the limits and test coverage.

### How the spend cap works

Every paid launch requires `--max-spend <USD>`. If the budget cannot cover the download, startup, ten minutes of use and the watchdog reserve, ablit refuses to rent and shows the minimum budget.

The quote prints the following notice, with the marketplace details above added where applicable:

```text
How the spend cap works: a watchdog on the rented machine destroys it when
spend reaches $<cap>, and ablit checks too while it is running on this computer.
If the rented machine freezes or loses its network while this computer is off
or asleep, neither check can run, and the marketplace keeps billing until you
run `ablit down` or the rental ends. Check `ablit status` if you leave a run
unattended.
```

## Commands

| Command | What it does |
|---|---|
| `ablit plan <model>` | Compares offers and full costs without renting. |
| `ablit up <model> --max-spend <USD>` | Quotes, asks for approval, rents and keeps the tunnel open. |
| `ablit status [run]` | Shows the run state and estimated spend. |
| `ablit logs [run]` | Reads logs with known secrets removed. |
| `ablit tunnel [run]` | Reopens the tunnel and resumes the local cap check. |
| `ablit down [run]` | Destroys the rental and confirms that billing has stopped. |

`plan` and `status` accept `--json`. Agents should read [AGENTS.md](AGENTS.md) for exit codes, readiness checks and renting with `--yes`. The [command reference](docs/cli-reference.md) lists every flag.

## Privacy and keys

ablit runs on your computer, has no telemetry and relays no inference traffic. Marketplace keys stay in the OS keyring or the corresponding `VAST_API_KEY`, `RUNPOD_API_KEY`, `CLORE_API_KEY` or `LIUM_API_KEY` environment variable.

The engine listens on `127.0.0.1` on the rented machine, and the SSH tunnel checks the host key after first contact. Paid launches accept only `--endpoint tunnel`. The endpoint has its own random key, stored in your keyring.

Renting gives you control over the pinned build and its logs. The marketplace and hardware owner can still inspect the machine, so a rental does not make your prompts private from them. A hosted API may be simpler and cheaper for occasional use.

## Paid links

ablit has a referral ID for Vast.ai and none for RunPod or Clore. It has none for Lium either. If you create a new Vast account through the optional paid link, the maintainer earns 3% of your spending as Vast credits for the life of the account (checked 2026-10-21).

Setup offers a plain link alongside the paid one. Commissions never affect offer ranking or your prices, and `ablit setup --no-referral` turns the links off. [POLICY.md](POLICY.md) has the rules and the tests that enforce them.

## Documentation and contributing

Read the [command reference](docs/cli-reference.md) for usage, the [architecture](docs/architecture.md) for implementation details, and the [design decisions](docs/design-decisions.md) for scope. [CONTRIBUTING.md](CONTRIBUTING.md) explains how to add a catalog entry or record a boot test. Report security issues through [SECURITY.md](SECURITY.md).

The code is licensed under [Apache-2.0](LICENSE), and the catalog data under [CC0-1.0](catalog/LICENSE). Each model has its own license, recorded in its catalog entry.
