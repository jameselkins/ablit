# Boot test: llama31-8b-abliterated-q4km on Lium, 2026-10-26

The maintainer ran this test with `ablit up --backends lium` on their own Lium account. Pod IDs, IP addresses, prices and keys are left out.

## Machine

One RTX 3090 on Lium (verified tier, region RO), with the pinned llama.cpp server image and the profile in the catalog entry.

## What happened

1. Lium reported the pod as starting after create. Its SSH address appeared about 2 minutes later.
2. ablit connected over SSH, ran the bootstrap, downloaded and verified the 4.9 GB GGUF file, and started llama-server.
3. The template needed an entrypoint override (`/bin/sleep` with `startup_commands: infinity`). Without it, Lium runs the image's own entrypoint (`/app/llama-server`) with the start command as arguments, and the container exits at once.
4. An 88-token chat completion came back through the SSH tunnel in 1.4 seconds wall time, which is about 63 tokens per second end to end.
5. `ablit down` deleted the pod and confirmed that billing stopped. The pod list was empty afterward. Total estimated spend was about $0.35 at $0.40 an hour.

## What failed first

An earlier run the same day rented with no entrypoint override. Lium reported `pod-create.failed` ("the image's default command exiting right after start", failure step `add_public_keys`) and the pod went `CREATION_FAILED`, which Lium does not bill. Lium also refuses shell metacharacters in the start command and environment values, so the key install and sshd setup cannot live in the start command; the rent call carries the public key instead. Lium has no scoped API keys, so on Lium the spend cap is enforced only by ablit on your computer plus the whole-hour `termination_hours` the driver sets from the cap.
