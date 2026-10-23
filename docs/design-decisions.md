# Design decisions

This page records why ablit works the way it does, so that a contributor can tell a deliberate choice from an accident. For how the pieces fit together, read [architecture.md](architecture.md).

## ablit runs on the user's computer

ablit uses the user's own marketplace accounts and keeps their keys on their computer. It runs no service, holds no money and relays no inference traffic, so there is nothing for a user to trust except the code they install. The cost is that ablit cannot act while the computer is off, and the spend cap notice says so.

## Referral commissions never affect the ranking

The maintainer may earn a commission when a new user signs up through a project link. To keep that from steering the ranking, referral IDs live in `cli/ablit/referral/ids.toml`, and the planner and the drivers cannot import the package that reads them. POLICY.md lists the rules and the tests, and it holds the exact disclosure text. No telemetry exists, and ablit prints links without opening a browser.

## The default endpoint is an SSH tunnel

The engine listens on `127.0.0.1` on the rented machine, and ablit reaches it through an SSH port forward that checks the host key. A public port would send the API key and the prompts over plain HTTP, and TLS on a public endpoint is out of scope. For that reason a paid `up` accepts only `--endpoint tunnel`.

## Every deadline is ablit's own

No marketplace gives ablit a deadline that it can rely on, so ablit applies a 20 minute start deadline and a five minute SSH deadline itself, and it destroys the run when either passes. The values come from observed behavior. A Vast instance once stayed in "loading" for over 32 minutes, and sshd normally answers within a minute or two.

## Whole Clore servers and Secure Cloud only

ablit rents whole Clore servers only. It drops RunPod Community Cloud offers from paid runs, because those pods may have no public IP for SSH.

## The Xolotl download keeps the machine busy

The Xolotl bootstrap overlaps the download with the work that follows it, because the 372 GB download is most of the bill. The download client is `engines/lib/fetch.py`, and it uses only the Python standard library, so it also runs in the llama.cpp image. It works as follows.

1. The Xolotl bootstrap cuts a file of 512 MiB or more into chunks of 256 MiB. Each chunk is one HTTP range request, and the client rejects any answer that is not 206 Partial Content, so a server that ignores the range cannot write wrong bytes.
2. Two large files download at once, with up to 16 chunks of each in flight, which is 32 connections. The settings are `ABLIT_DOWNLOAD_FILES`, `ABLIT_DOWNLOAD_RANGES`, `ABLIT_DOWNLOAD_CHUNK_BYTES` and `ABLIT_DOWNLOAD_SPLIT_BYTES`, and the top of `fetch.py` lists their defaults.
3. A failed chunk is retried with growing waits, and it resumes from the bytes already written. A chunk that makes no progress for the stall time is dropped and retried.
4. One thread per file hashes the finished beginning of the file while the later chunks still download, so a file needs no second read. The file is renamed into place only when its size and SHA-256 match the catalog, and a mismatch fails the run with the reason `hash_mismatch`.
5. The state of each part is kept next to it, so a restart resumes at the chunk level and never repeats a verified file.

In the default mode, `ABLIT_ENGRAM_MODE=direct`, the order of work is as follows.

1. The speed check runs the real client with the real connections on the first weight parts for 45 seconds, and the bytes it downloads are kept.
2. The small files download next, which are the index, tokenizer, rank, config, expert map, checksum and script files.
3. The weight parts and the draft model follow. A join worker started before the download appends each part to `joined/model.gguf` as soon as the part is verified, then deletes it, so the join finishes a few seconds after the last part.
4. The two Engram source files download last. They need no work after they are verified, so the end of the download has only the hash of the final chunks behind it.
5. The engine starts when every file is verified and joined, and it reads the two official Engram files in place.

With `ABLIT_ENGRAM_MODE=extract`, the two Engram source files come first, and an extraction worker starts as soon as both are verified. It runs `extract_engram.py` while the weight parts download, and it needs about 203 GB of extra disk, which the disk request already includes. The extraction also runs as an automatic fallback when the engine exits at start with an Engram open error in direct mode. The boot tests on 2026-10-21 and 2026-10-22 ran in direct mode with no extraction.

The pre-extracted Engram tables in `kaushikvira/DeepSeek-V4.1-Flash-1017-GSQ-agentic-strata` are not used. Their headers, sizes and index match a regeneration from the official 1017 shards, and 602 sampled 1 MiB blocks per tensor were identical, but the check was not a hash of all 203 GB. The September shards differ from the 1017 shards in every sampled block, so the September entry cannot use them. Direct mode already removes the copy, so the tables would add a third party to the trust path and save nothing.

## Not in scope

The following are left out on purpose.

- Launching GGUF files that are not in the catalog.
- SkyPilot and dstack export, and third-party driver plugins.
- Automatic re-planning when a machine fails.
- TLS on a public endpoint.
- Hosted price data or price history, and telemetry.
- Multi-node serving, and engines other than vLLM, llama.cpp and Xolotl.
- A web interface.
