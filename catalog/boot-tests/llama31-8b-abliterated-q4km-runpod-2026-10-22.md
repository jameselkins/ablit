# Boot test: llama31-8b-abliterated-q4km on RunPod, 2026-10-22

The maintainer ran this test with `ablit up --backends runpod --trust verified` on their own RunPod account. Pod IDs, IP addresses, prices and keys are left out.

## Machine

One RTX 2000 Ada on RunPod Secure Cloud, with the pinned llama.cpp server image and the profile in the catalog entry.

## What happened

1. RunPod reported the pod as running about 20 seconds after create. Its direct SSH address appeared about 40 seconds later.
2. ablit connected over SSH, ran the bootstrap, downloaded and verified the 4.9 GB GGUF file, and started llama-server.
3. A 400-token chat completion came back through the SSH tunnel in 9.9 seconds, which is about 40 tokens per second end to end.
4. A request without the endpoint key got HTTP 401.
5. `ablit down` terminated the pod and confirmed that billing stopped. The pod list was empty afterward.

## What failed first

An earlier run the same day used a Community Cloud offer. The pod never got a direct SSH address, so ablit could not reach it, and the start deadline destroyed it after 20 minutes. The likely cause is a host without a public IP. RunPod has no scoped API keys, so on RunPod the spend cap is enforced only by ablit on your computer.
