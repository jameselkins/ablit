# DeepSeek V4 boot and Claude Code tests on Vast, 2026-10-27

The maintainer approved a $5 total budget for Nanbeige and DeepSeek coding tests. The runs used the catalog's pinned Xolotl image, build and profile. Host IDs, offer IDs, IP addresses and keys are omitted.

## Tested build

| Field | Value |
|---|---|
| Image digest | `sha256:5d36c97c39cd195723a9a1f7a1a0c8875ab2a87551d0ab2db4efb68a20e0fb69` |
| Build SHA-256 | `114da1005eac13c715ccf938bd70340d24884618168956c6e8474e0ed8d560df` |
| Bootstrap SHA-256 | `f5fbd1be3883ba8cccb9edde764c40db3b7d717ba7c45d64d0772d84fa5ee9c6` |
| Context | 131,072 tokens |

## RTX 3090 Ti boot test

The first run used one RTX 3090 Ti with 24.564 GB of VRAM and 128.489 GB of host RAM on a verified Vast host. The run tested the entry's 24 GB GPU floor, with more host RAM than its 90 GB floor.

The bootstrap downloaded and verified 371.7 GB of artifacts, joined the weights and completed all nine stages. Measured download speeds were about 3 to 4.2 Gbps. The engine passed its API health checks and reported a context of 131,072 tokens.

The SSH tunnel closed after readiness, before a Claude Code session could start. The cause is unknown because that version discarded OpenSSH's error output. The CLI now saves the output in a private `tunnel.log`, but the change does not claim to fix the connection failure.

ablit automatically destroyed the rental and confirmed that billing stopped. Its estimated spend was $1.65, including estimated transfer charges. The run proves boot and API health on a 24 GB GPU, and it provides no coding result.

## RTX 5090 retry

A retry used one RTX 5090 with 32.607 GB of VRAM and 192.95 GB of host RAM on a verified Vast host. SSH never answered within five minutes of the marketplace reporting the machine as running. ablit destroyed the rental and confirmed that billing stopped.

The estimated spend was $1.11. The estimate includes the quoted full download charge even though the bootstrap never ran, so it is not a final provider invoice. No boot or coding result came from the retry.

## RTX 4090 coding attempt

A third run used one RTX 4090 with 24.564 GB of VRAM and 128.657 GB of host RAM on a verified Vast host. The download ran at about 5.8 to 7.9 Gbps. All nine stages completed, and the engine served the catalog's context of 131,072 tokens.

Claude Code 2.1.311 started the [search coding task](../../docs/claude-code-smoke-test.md), using a fresh fixture and config directory. It read the files, changed the search implementation and added six regression tests. The model's code preserves all six original test assertions. An independent run after the interruption passed all 12 tests and the three separate search checks.

The session did not return a successful Claude Code result. The local computer slept with its lid closed, the SSH tunnel exited with code 255, and the test runner reached its 900-second limit. The saved SSH error says the remote host closed the connection, but it does not establish the cause. The interrupted attempt is not a pass or a useful timing benchmark. The follow-up prompt never started.

ablit destroyed the rental and confirmed that billing stopped, with no remaining resources or pending cleanup. Its last recorded estimate was $0.42. Counting compute through the final stopped record gives a more conservative allowance of about $0.50, and neither figure is a provider invoice.

The combined Nanbeige and DeepSeek tests used about $3.85 under that conservative calculation. The remaining budget could not cover the replacement offers available after the interruption, so no further machine was rented. A complete DeepSeek Claude Code session remains unverified.
