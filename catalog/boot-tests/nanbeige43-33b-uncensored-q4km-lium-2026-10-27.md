# Nanbeige Claude Code tests on Lium, 2026-10-27

The maintainer approved a $5 total budget for Nanbeige and DeepSeek tests. The Nanbeige coding test used an RTX A6000 with 48 GB of VRAM and 47 GB of host RAM on Lium. Host IDs, offer IDs, IP addresses and keys are omitted.

## Tested build

| Field | Value |
|---|---|
| Image digest | `sha256:c65f01343624f55848a82a6b653880e53399377a92b371975eb3303c1d5647e7` |
| Build SHA-256 | `57f31616f661f60e0f702bf2eddf068bad2e2e722041e7ecee0f24b702b6adb7` |
| Bootstrap SHA-256 | `c5d30667801c4c551500b71c20858ca405c8de120f21f679cb36e393bb9932ac` |
| Context | 32,768 tokens |
| Claude Code | 2.1.311 |

The model, image and bootstrap match the catalog entry. The run completed all nine bootstrap stages, and the download ran at about 3.7 to 4.8 Gbps.

## Interrupted first Lium run

The test used the [search fixture and prompts](../../docs/claude-code-smoke-test.md), with fresh Claude Code config and all model aliases pointing to the local rented endpoint. There were no MCP servers, browser tools or hosted API key. The model could read and edit the fixture and run its unit tests.

The first prompt completed successfully in 411 seconds. Claude Code read the files, fixed search behavior and added 12 regression tests while preserving the six original tests. Its test run passed all 18 tests. An independent test run and separate checks for case, punctuation, repeated words and result ordering also passed.

The first round made 12 tool requests. One optional Python command was refused by the test's shell permissions, which permit only `python3 -m unittest*`. The model continued and completed the required test command. The endpoint reported 9,638 input tokens, 94,356 cached input tokens and 11,664 output tokens across the round's requests. The token figures are engine reports, and they are not hosted API charges.

The resumed follow-up added `Index.add` and `Index.remove`. The edited code passed the independent replacement, removal and search checks, but the session ended with a connection-refused error after 531 seconds. ablit had detected a closed SSH tunnel and destroyed the rental. macOS recorded a nearly five-minute sleep during the follow-up, followed by the tunnel failure after wake. The interrupted follow-up is not counted as a completed Claude Code task.

ablit confirmed that billing stopped. Estimated spend for the Lium run was $0.15.

## Successful retry

A fresh run on the same GPU model and host RAM completed both prompts in one resumed Claude Code session. It started from a fresh copy of the fixture, using the same build, profile, tool permissions and test limits.

| Prompt | Wall time | Final unit tests | Independent checks | Claude Code result |
|---|---|---|---|---|
| Search fix | 423.8 seconds | 14 passed | Passed | Success |
| Add, replace and remove | 227.4 seconds | 20 passed | Passed | Success |

The model preserved the six original tests and added eight search regression tests, then six tests for the follow-up. It made 12 tool requests in the first round and four in the second. One request for git history and a parent directory listing was refused by the test's permissions. The model continued without it.

The endpoint reported 7,028 input tokens, 61,055 cached input tokens and 12,133 output tokens for the first round. The resumed round reported 3,712 input tokens, 104,674 cached input tokens and 5,912 output tokens. The figures are totals across requests in each round, rather than the size of one prompt.

`ablit down` confirmed that billing stopped, with no remaining resources or pending cleanup. The retry's estimated spend was $0.10. Both completed tasks are small compatibility tests, and they do not establish performance on a large codebase.

## Earlier Vast attempts

The first Vast RTX 5090 failed the measured download speed check at 423 Mbps, below the allowed floor. The second remained in the marketplace's loading state and was stopped after about 12 minutes. Neither reached an endpoint, and both were confirmed stopped. Their combined estimated spend was $0.34.

The failed starts and interrupted follow-up are kept in the record so the successful retry does not imply that every attempt completed.
