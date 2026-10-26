# Contributing

Thank you for helping. The most useful contribution is a catalog entry for a model that people want to run, and the second most useful is a boot test of an entry that nobody has booted yet. This file explains both, along with the checks that every pull request has to pass.

## Rules that apply to everything

1. CI never rents a machine. The workflows use no secrets, ask for read-only repository permission, and make only anonymous requests to Hugging Face. A pull request that adds a step that rents anything, or that reads a secret, will be closed.
2. Sign off every commit with `git commit -s`. The sign-off adds a "Signed-off-by" line with your name and email address, and it certifies the Developer Certificate of Origin version 1.1 at https://developercertificate.org. In short, it says that you wrote the change or have the right to submit it under this project's license.
3. Code is licensed under Apache-2.0. Catalog entries are released under CC0-1.0, and an entry records the license of its model separately.
4. Write every file yourself. Do not paste code or templates from another project unless its license allows it and you say where it came from.
5. Write prose in plain, everyday words, in complete sentences, with sentence case headings.

## Adding a catalog entry

An entry is one YAML file at `catalog/models/<id>.yaml`. The file name must match the `id` field. Entries hold data only, and no entry may carry a shell command. All commands live in the reviewed engine templates under `engines/`.

1. Pick the Hugging Face repo and copy the 40-character commit hash of the revision you want. A branch name such as `main` is refused, because a pinned revision keeps the entry reproducible. The repo must be public and not gated.
2. Copy a seed entry that uses the same engine, and change `id`, `display_name` and the `source` block. `modification` is `none`, `abliterated`, `finetune` or `quantized`, and `license` is the license the model's own repo declares.
3. List every file that the engine reads under `artifacts`. That includes draft models, tokenizer files and any extra source shards. Give each file a `path` and a `role`, and leave `bytes` as `null`. A file from another repo gets its own `repo` and `revision`. Do not list files that the engine does not read, and for repos that hold several builds list only the build the entry is for.
4. Run `python3 tools/verify_entry.py --fill catalog/models/<id>.yaml`. It reads the Hugging Face API at your pinned revision and writes each file's size and SHA-256. A file stored without LFS gets its Git blob ID too, and the script downloads it to compute the SHA-256. The script rewrites the file in a fixed layout, and it keeps only the comment lines at the top.
5. Write `staging`, which lists what happens to the files on the rented machine, in order. A `download` step brings in files, either `all` or a list of paths and roles. A `join` or `engram-extract` step names its `inputs` by path or role, gives the `output_bytes` it writes, and says with `delete_inputs_after` whether the inputs are removed when it finishes. `keeps_inputs_until_done: false` is allowed only for a step that is known to stream, which means it deletes each input before it reads the next. The Xolotl bootstrap joins weight parts this way, so its entries say `false` on the join. The peak the engine lock adds as `disk_margin_gb` covers the one part in flight. ablit computes the peak disk from this list, and nobody writes it by hand.
6. Fill in `profile`. It is one exact combination of engine lock, GPU arrangement, context length and concurrency, and a boot test of one profile does not approve another.
7. Under `sources`, add the URL that supports each number in `profile`. A source can be a bare URL, or an object with `url` and `covers`, where `covers` lists the profile paths the URL supports, such as `profile.gpu.count`. A trailing `*` matches a group. Use primary sources, which means Hugging Face, the engine's own repository, or the engine's documentation. Local file paths are not allowed.
8. Set `status: unverified` and `tests: []`. Add a `notes` list that says, in plain words, where each figure came from, which ones you derived, and that the hardware floors are not yet tested.
9. Run the checks below, and open the pull request.

### Numbers chosen by ablit

Some numbers have no publisher. Examples are the context length, the number of concurrent requests, the download speed floor and the startup estimate. When no source states a figure, the entry may use a default or a figure derived by arithmetic, as long as a `notes` item says so. Cite the URL of this section in `sources` and list the profile paths under `covers`, with a `note` that says the figure is a default or a derivation. The defaults are 4096 tokens of context, one concurrent request, and a download floor of 500 Mbps. An entry whose download is larger than 100 GB uses a floor of 1000 Mbps, because the download then decides most of the cost. For 372 GB, 500 Mbps takes about 99 minutes and 1000 Mbps takes about 50 minutes (372 GB times 8000, divided by the speed in Mbps, divided by 3600). The startup estimate counts only the time after the last downloaded byte, because the quote counts the download separately. For an engine with its own image, it is 0.25 hours (the figure of the boot-tested llama.cpp entry, which covers the image pull, the container start and the health check), plus the longest engine load time the model cards report, plus 60 seconds for the last join and checksum, rounded up to one decimal. For the Xolotl entries that is 0.25 hours plus 92 seconds plus 60 seconds, which is 0.292 hours and rounds up to 0.3. It is a guess until a boot test measures it.

## Checks that CI runs

CI runs these checks on every pull request. None of them rents anything.

1. The file passes `catalog/schema/v1.json`.
2. `tools/verify_entry.py` confirms that each repo and revision exist, and that each artifact's size and hash match the Hugging Face API at `?blobs=true`. The tree API is paged at 50 entries, so it is not used.
3. Every number in `profile` has a source.
4. The computed peak disk is no larger than any disk figure the entry claims in `profile.storage.disk_gb_claimed`.
5. For an entry that uses llama.cpp, the GGUF `general.architecture` is on the pinned llama.cpp build's list. The check is skipped until `engines/llamacpp/lock.yaml` lists the supported architectures.

On a fork, where the network may be unavailable, run the schema part alone with `python3 tools/verify_entry.py --offline`. To check the whole repository, run `uv run --with-editable . --with pytest --with hypothesis --with respx --with import-linter pytest -q` from the top of the repository, run `lint-imports` there too, run `python3 tools/verify_entry.py --offline` for every entry, and run `python3 tools/build_catalog.py`.

The shell tests need Bash 3.2 or newer, including the Bash that ships with macOS. CI runs the full test suite on Linux and macOS. It also builds the wheel and checks every launchable entry's bootstrap bundle from a clean installation with `python -I tools/check_install.py`. The installation check makes no network requests and rents nothing.

A weekly job runs check 2 again on every entry, and it fails when an artifact has disappeared. A maintainer then sets that entry to `removed`.

## Entry status

| Status | Meaning |
|---|---|
| `unverified` | The entry passed the checks and a maintainer merged it. Nobody has booted it. `ablit up` needs `--untested` to launch it, and the quote says "Nobody has booted this entry yet. The engine may fail to start, and you pay for the time until ablit destroys the run." |
| `boot-tested` | The `tests` list holds a record with a log from a real run of that exact profile. You write the record from the output of `ablit logs`, which removes secrets, and you decide whether to share it. The `--report` flag of `ablit up` is accepted but does not write a log yet. |
| `removed` | An artifact has disappeared or the entry is withdrawn. ablit refuses to launch it. |

Before it rents anything, ablit also checks that every artifact of the entry is still public at its pinned revision.

## Recording a boot test

A boot test is a real run of an entry's exact profile, paid for from your own marketplace account. It is the second most useful contribution, and it needs a budget that you approve yourself, because CI never rents anything.

1. Run `ablit up <id> --untested --max-spend <USD>` for an `unverified` entry, and send a request through the tunnel to check that it answers.
2. Save the bootstrap log with `ablit logs --stage bootstrap`, and note the machine's GPU, the stage times, the measured download speed, the speed of a chat completion in tokens per second, and whether `ablit down` confirmed that billing stopped.
3. Write a record in `catalog/boot-tests/<id>-<date>.md`, in the layout of the existing records there. Leave out host IDs, offer IDs, prices and keys.
4. Add `{backend: <name>, date: "<YYYY-MM-DD>", log: boot-tests/<file>}` to the entry's `tests` list, set `status: boot-tested`, and open a pull request.

## Improving the code

The Python package is in `cli/ablit/`, and [docs/architecture.md](docs/architecture.md) explains how its parts fit together. [docs/cli-reference.md](docs/cli-reference.md) lists the commands and flags, [docs/catalog-format.md](docs/catalog-format.md) describes the entry format, and [docs/design-decisions.md](docs/design-decisions.md) gives the reasons for the main choices. Change the matching page when a change alters what it says.

Run the tests before you open a pull request. CI also runs `ruff check` and `mypy`, and `.pre-commit-config.yaml` runs ruff and gitleaks before each commit when you install it with `pip install pre-commit && pre-commit install`. The five tests that enforce the rule against steering by commission are in `cli/tests/test_steering.py`, and POLICY.md explains them. A change that lets referral data reach the planner will not be accepted.

Engine scripts live in `engines/<name>/` with shared code in `engines/lib/`. They follow these rules, and `cli/tests/test_hygiene.py` checks most of them.

1. Each script starts with `#!/usr/bin/env bash` and sets `set -euo pipefail`.
2. The engine listens on `127.0.0.1` and never on `0.0.0.0`.
3. The endpoint key arrives only as an environment variable. It is never put on a command line, echoed or written to a file.
4. Each bootstrap stage writes a marker under `/ablit/stages/`, so a restart skips finished stages.
5. A lock file names its image by digest, and ablit refuses to rent when the digest is null.

Scripts that have not run on a rented machine say `untested on a rented machine`. Synthetic coverage does not change a catalog entry to `boot-tested`.

### Testing create recovery

`ABLIT_FAULT=drop-create-response` discards a successful Vast create response before the instance ID is saved. The durable record still says create was sent. The next `ablit up` for the same model reconciles the label, and `ablit down <run>` can find and destroy the instance. Neither command sends another create for that run. Unset the variable after the fault. It has no effect unless explicitly set.

Tests use `respx`, invented marketplace responses and a fake OS keyring. No test rents a machine. Run the complete test command above and `lint-imports` before a paid check. Paid checks require a separate approved budget and the maintainer's own account.

The fault hook exercises a paid create when used against Vast. Run it only as part of an approved paid test. A lost watchdog-key or SSH-key response has no safe automatic lookup in the documented schema, so cleanup stays pending for console review.
