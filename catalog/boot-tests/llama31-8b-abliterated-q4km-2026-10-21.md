# Boot test: llama31-8b-abliterated-q4km on Vast, 2026-10-21

The maintainer ran this test with `ablit up` on their own Vast account. Host IDs, offer IDs, prices and keys are left out.

## Machine

One RTX 3060 with 12 GB of VRAM, rented on Vast with the pinned llama.cpp server image and the profile in the catalog entry.

## Stage times from the bootstrap log

| Stage | Time |
|---|---|
| Start, checks | 1 s |
| Download speed check | 44 s, measured 883 Mbps |
| Download and verify the 4.9 GB GGUF file | 9 s |
| Staging | none needed for one GGUF file |
| Start llama-server | 10 s |
| Health check and ready | under 1 s |

The bootstrap took 64 seconds from start to ready. The time before the bootstrap started, while Vast pulled the image and started the container, is not in this log.

## Results

1. A chat completion came back through the SSH tunnel at about 65 tokens per second.
2. A request without the endpoint key got HTTP 401.
3. `ablit down` destroyed the machine, deleted the run's scoped watchdog key and SSH key, and confirmed that billing stopped.

Two more paid tests on the same day covered the run lifecycle. In one, the watchdog on the machine destroyed a run when it reached its cap, and ablit exited with code 8. In the other, ablit recovered a run whose create response was dropped on purpose, without sending a second create.
