# ablit

ablit is a command-line tool that finds the cheapest rented GPU machine that can run an open-weight language model, shows the full cost, and starts the model on your own marketplace account. When the machine is ready, ablit gives you a local OpenAI-compatible endpoint, and `ablit down` destroys the machine and confirms that billing has stopped.

It is built for abliterated and uncensored models, which are models edited to remove their refusals. ablit runs on your computer, so your marketplace key stays with you, and ablit runs no service, holds no money and relays no inference traffic.

Status: alpha. Three models have run end to end on rented machines, and the table in the Status section lists what was tested and when.

## DeepSeek-V4.1-Flash 1017 on one GPU works

On 2026-10-22, ablit rented one RTX 5090 (32 GB) on Vast and ran `ds41-1017-xq-v3-uncensored`, an uncensored quant of the 552B DeepSeek-V4.1-Flash 1017 model, on the Xolotl engine. The machine was ready in under 27 minutes, including the 372 GB download, and it answered at about 33 tokens per second through the SSH tunnel. The September build of the same model, `ds41-september-gsq-uncensored`, ran the day before at about 47 tokens per second on a 98 GB card. Neither has been tested on a 24 GB card yet, which is the smallest card the engine author reports.

```
ablit setup vast
ablit up ds41-1017-xq-v3-uncensored --max-spend 4
```

## Why run your own model instead of a hosted one

Several hosted services already sell uncensored models through an API, and for light use they are cheaper and simpler than ablit. Some of them also ask who you are before you can use the model, e.g., Morph requires KYC and a stated purpose for its abliterated DeepSeek. For example, Refuseless serves DeepSeek V4.1 Flash 1017 abliterated for $0.30 per million input tokens and $0.85 per million output tokens, while ablit's cheapest one-GPU machine for the 1017 model cost $0.67 an hour on 2026-10-22, before the download time. One person sending requests one after another would rarely spend $0.67 an hour on tokens.

ablit is for cases where you want control over the model more than the lowest price. On a hosted service, the provider picks the build, can change or remove the model, and sets its own logging rules, which you can read but not check. With ablit, you pick the exact build by its Hugging Face revision, the weights and logs sit on a machine you rented, and nobody can swap the model while you use it. The cost is also fixed by the hour, so heavy use does not grow the bill.

| Service | What it sells | Logging, from its own pages | Can the provider change the model |
|---|---|---|---|
| [Refuseless](https://refuseless.com) | API for abliterated GLM 5.3 and DeepSeek V4.1 Flash 1017, priced per token ([pricing](https://refuseless.com/pricing)) | Prompts are "never saved" ([privacy](https://refuseless.com/privacy)) | Yes ([terms](https://refuseless.com/terms)) |
| [abliteration.ai](https://abliteration.ai) | API and chat, per token or $20 to $200 a month ([pricing](https://abliteration.ai/pricing)) | Content is not stored, and token counts and timestamps are kept ([data handling](https://abliteration.ai/data-handling)) | Yes ([terms](https://abliteration.ai/terms-of-service)) |
| [Experiential Labs](https://experientiallabs.ai/) | A model gateway with uncensored GLM-5.3 Flash and DeepSeek V4.1 Flash, per token ([uncensored](https://experientiallabs.ai/uncensored)) | The DeepSeek model is "not zero retention", and the standard service keeps prompts for 30 days ([privacy](https://experientiallabs.ai/privacy)) | Yes ([terms](https://experientiallabs.ai/terms)) |
| [Morph](https://morphllm.com) | DeepSeek V4.1 Flash 1017 Abliterated through its model API, open only to customers who pass KYC and explain their purpose ([announcement](https://x.com/morphllm/status/2113174908167712865)). Its docs list the regular 1017 model at $0.12 in and $0.80 out per million tokens ([docs](https://docs.morphllm.com/llms.txt)), and the abliterated price is not listed. | Not checked | Not checked |
| [Lyber](https://www.lyber.ai/) | One OpenAI-compatible API that routes to five uncensored or abliterated models (Qwen 3.8 27B, DeepSeek V4.1 Flash, GLM 5.3 Full, GLM 5.3 Flash and Kimi K3), on prepaid per-token credit, listed from $0.25 to $5.00 per million input tokens and $1.00 to $15.00 per million output tokens. It is in early access, and you request access ([models and pricing](https://www.lyber.ai/)). | Not checked | Not checked |
| ablit | Rents a machine on your own Vast or RunPod account and runs a pinned build | The engine runs on the rented machine with its text log turned off, and ablit has no telemetry | No, the build is pinned by revision and hash |

The services' details come from their own pages, read on 2026-10-22 (Lyber on 2026-10-23), and ablit has not tested them. A rented machine is not private from the marketplace or the machine's owner, because Vast and RunPod hosts run the hardware and can see what runs on it. A 552B model on one card is also slower than a hosted model on a large cluster.

## Why Xolotl, and what it is good for

DeepSeek-V4.1-Flash has 552B parameters, so the usual way to run it is a node with 8 GPUs of 80 GB or more. Such nodes are few and expensive. On 2026-10-22, `ablit plan` found 13 offers across Vast, RunPod and Clore that could run the full FP8 model, and the cheapest cost $10.81 an hour, or about $287 for 24 hours. For the one-GPU Xolotl build, it found 26 offers, and the cheapest cost $0.67 an hour, or about $17 for 24 hours.

[Xolotl](https://github.com/Indras-Mirror/Xolotl) is a research engine by one developer that runs the model on one consumer GPU with about 90 GB of RAM. The model is a mixture of experts, so each token uses only a few of its experts. Xolotl keeps the most used experts in GPU memory and the rest in RAM and on disk, and it loads the others when a token needs them. Its quant files load only in Xolotl.

Xolotl fits when you want the large DeepSeek model and an 8-GPU node is too expensive or not available, and when about 30 to 50 tokens per second for one user is enough. It does not fit a service with many users. The pinned engine commit has batch slots that let one engine serve several requests at once, but ablit leaves them off, because each slot uses more GPU memory and ablit has not tested them. The engine is new, it has one main author, and its speed figures come from a few machines, so treat it as research code.

## Quickstart

ablit needs Python 3.11 or newer and OpenSSH.

```
uv tool install ablit
ablit plan llama31-8b-abliterated-q4km
ablit setup vast
ablit up llama31-8b-abliterated-q4km --max-spend 2
ablit down
```

First, `ablit plan` is free and needs no account. It prints the cheapest machine that meets the model's needs and the next four, with the full cost for 1 hour, 24 hours and 7 days. Second, `ablit setup vast` takes your Vast API key with hidden input, tests it, and can save it in the OS keyring. Third, `ablit up` shows the quote, asks you to type `rent`, rents the machine, starts the engine, and prints a local endpoint. Last, `ablit down` destroys the machine and confirms that billing has stopped.

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

The cap counts compute, disk, the model download and transfer charges. If the cap cannot cover the download, the startup estimate and 10 minutes of use, `ablit up` exits with code 5 and shows the smallest cap that would work. RunPod and Clore have no API keys limited to one machine, so on those marketplaces only the check on your computer enforces the cap. `ablit plan` searches Clore without a key, and `ablit up` includes Clore only when you have saved a Clore key.

## Status

| What | Tested on a rented machine |
|---|---|
| `llama31-8b-abliterated-q4km` on llama.cpp | Yes, on Vast on 2026-10-21 (about 65 tokens per second) and on RunPod Secure Cloud on 2026-10-22 (about 40 tokens per second) |
| `ds41-september-gsq-uncensored` on Xolotl | Yes, on Vast on 2026-10-21, on a 98 GB card |
| `ds41-1017-xq-v3-uncensored` on Xolotl | Yes, on Vast on 2026-10-22 on a 32 GB RTX 5090 (about 33 tokens per second), and on 2026-10-23 with the newer engine pin on a 98 GB card (about 52 tokens per second) |
| The watchdog destroying a run at its cap, and recovery of a lost create response | Yes, on Vast on 2026-10-21 |
| `llama31-8b-abliterated-q4km` on llama.cpp | Yes, on Lium on 2026-10-26 (about 63 tokens per second on an RTX 3090) |
| vLLM entries, spot machines, public endpoints and Clore rentals | No |

The records are in `catalog/boot-tests/`. The two vLLM entries can be planned, and `ablit up` refuses them until their image is pinned and tested.

## Marketplaces

| Marketplace | Search | Renting with `ablit up` |
|---|---|---|
| [Vast.ai](https://vast.ai) | Yes, without a key | Yes, tested. A watchdog key limited to the one machine enforces the cap on the machine. |
| [RunPod](https://www.runpod.io) | Yes, without a key | Yes, tested on Secure Cloud. Community Cloud pods may get no public IP for SSH, so paid runs use Secure Cloud only. |
| [Clore.ai](https://clore.ai) | Yes | Connected with `--backends clore`, and untested on a rented machine. Clore has no scoped keys, and the marketplace does not say which servers allow SSH, so ablit cancels a server that has no SSH port at the start deadline and skips it after that. |
| [Lium](https://lium.io) | Yes, without a key | Connected with `--backends lium`, and tested on a rented machine on 2026-10-26. Keys made with your key cannot be limited to one machine, so the cap relies on the check on this computer and on Lium deleting the pod after a whole number of hours. Notes and sources are in `backends/lium.md`. |

## Catalog

The catalog is a folder of YAML files in `catalog/models/`, one per model build, checked by `catalog/schema/v1.json`. It keeps the best build for each runtime and hardware tier, not every build that exists. The catalog data is released under CC0-1.0, and each entry records the license of the model it describes.

| Entry | Runtime | Hardware | Status | Why it is here |
|---|---|---|---|---|
| `llama31-8b-abliterated-q4km` | llama.cpp | One small GPU | boot-tested | A cheap abliterated model for a first run |
| `ds41-september-gsq-uncensored` | Xolotl | One 24 GB GPU, 90 GB RAM | boot-tested | DeepSeek-V4.1-Flash on one GPU. The author's tests give it the best agentic scores of the one-GPU builds. |
| `ds41-1017-xq-v3-uncensored` | Xolotl | One 24 GB GPU, 90 GB RAM | boot-tested | The newer 1017 checkpoint on one GPU. The author reports it trails the September build on the hardest agentic tasks. |
| `ds41-1017-abl-fp8` | vLLM | 8 GPUs with 80 GB each | unverified | The abliterated 1017 model at full FP8 precision |
| `llama33-70b-fp8-dynamic` | vLLM | From its sources | unverified | An unmodified 70B model, kept as a cheaper first test of vLLM |
| `ornith15-122b-uncensored-iq2xxs` | llama.cpp | One 48 GB GPU, 64 GB RAM | unverified | Ornith-1.5-122B with refusals removed, in the 44 GB 2-bit build. Text only, and the publisher reports it complies more often but scores lower on MMLU than the Q4_K_M build. |
| `ornith15-122b-uncensored-q4km` | llama.cpp | One 84 GB GPU, 96 GB RAM | unverified | The same model in the 79 GB Q4_K_M build, which the publisher reports keeps more of the base model's MMLU score. Text only. |

The quality claims in the table come from the model authors, and ablit has not checked them. CONTRIBUTING.md explains how to add an entry.

## Runtimes

A runtime, or engine, is the server program that loads the model on the rented machine and answers requests.

| Runtime | What it is | Tested on a rented machine |
|---|---|---|
| [llama.cpp](https://github.com/ggml-org/llama.cpp) | A widely used C++ server for GGUF model files, run from its official CUDA image pinned by digest | Yes |
| [Xolotl](https://github.com/Indras-Mirror/Xolotl) | The one-GPU DeepSeek engine described above, run from an image ablit builds from a pinned commit | Yes |
| [vLLM](https://github.com/vllm-project/vllm) | A widely used Python server for full-size models across several GPUs | No |

## Commands

| Command | What it does |
|---|---|
| `ablit setup [vast\|runpod\|clore\|lium]` | Enters a key with hidden input, tests it, and can save it in the OS keyring |
| `ablit plan <model>` | Shows the chosen offer and the next four, with totals, and why other offers were dropped. Rents nothing. |
| `ablit up <model>` | Plans, asks you to type `rent` (or takes `--yes`), rents the machine, and keeps the tunnel open |
| `ablit status [run]` | Run state, resource IDs, estimated spend and cleanup |
| `ablit logs [run]` | Logs from the machine with known secrets removed |
| `ablit tunnel [run]` | Re-opens the SSH tunnel and runs the local cap check |
| `ablit down [run]` | Destroys the machine, confirms it is gone, and revokes run credentials. Run it again if it exits with code 7. |
| `ablit catalog list`, `show <id>`, `validate <file>` | Lists entries, explains one, or checks a contributor's file |

Agents and scripts should read [AGENTS.md](AGENTS.md), which covers `--json` output, exit codes, and renting with `--yes`. The [command reference](docs/cli-reference.md) lists every flag.

## The endpoint and your keys

The engine on the rented machine listens on `127.0.0.1` only, and ablit reaches it through an SSH port forward that checks the host key. ablit prints a local URL and the name of the keyring entry that holds the endpoint key, which is a random 32-byte value. Paid launches accept only `--endpoint tunnel`. Closing the tunnel does not stop billing, so run `ablit down` to stop the run. Marketplace keys come from the OS keyring or from `VAST_API_KEY`, `RUNPOD_API_KEY` or `CLORE_API_KEY`, and an environment key takes priority.

## Paid links

ablit has a referral ID for Vast.ai and none for RunPod or Clore. It has none for Lium either. If you have no Vast account, `ablit setup vast` shows a disclosure and offers the paid link, a plain link, or skip. The commission never changes your prices or which machine ablit picks, and `ablit setup --no-referral` turns the links off. POLICY.md has the full rules and the tests that enforce them.

| Marketplace | Commission the maintainer earns from a new account | Checked |
|---|---|---|
| Vast.ai | 3% of what you spend there, for the life of the account, paid by Vast as credits | checked 2026-10-21 |

## More documentation

| File | Contents |
|---|---|
| [AGENTS.md](AGENTS.md) | Using ablit from an agent or script |
| [docs/cli-reference.md](docs/cli-reference.md) | Every command and flag, the quote, and the exit codes |
| [docs/architecture.md](docs/architecture.md) | How the planner, the spend cap, the drivers and the engine bootstrap work |
| [docs/catalog-format.md](docs/catalog-format.md) | The catalog entry format and the rules for entry status |
| [docs/design-decisions.md](docs/design-decisions.md) | Why ablit works the way it does, and what it leaves out |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to add a catalog entry, record a boot test, or change the code |
| [POLICY.md](POLICY.md) | The referral rules and their tests |
| [SECURITY.md](SECURITY.md) | How to report a problem, and what ablit does with secrets |

## License

The code is licensed under Apache-2.0, and the catalog data under CC0-1.0. See LICENSE, NOTICE and `catalog/LICENSE`.
