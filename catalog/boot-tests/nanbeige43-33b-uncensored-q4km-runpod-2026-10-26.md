# Boot test: nanbeige43-33b-uncensored-q4km on RunPod, 2026-10-26

The maintainer ran this test with `ablit up --backends runpod --untested --max-spend 3 --yes` on their own RunPod account. Pod IDs, IP addresses and keys are left out.

## Machine

`ablit plan` chose one A40 with 48 GB of VRAM and 43 GB of host RAM on RunPod Secure Cloud, at $0.59 per hour ($0.5942 all-in). RunPod did not report a region. The machine used the pinned llama.cpp server image and the profile in the catalog entry.

## What happened

1. ablit created the pod, and RunPod accepted the order.
2. RunPod kept the pod in the `starting` state for the whole start deadline and never gave it a direct SSH address. ablit could not connect, so the bootstrap never ran and the GGUF file was never downloaded.
3. After 20 minutes, ablit destroyed the run and exited with code 6. It confirmed that billing stopped.
4. `ablit down` on the run exited 0, and `ablit status` showed the run as `billing_stopped_confirmed`.
5. ablit estimated the spend at $0.20 for about 20 minutes of the pod.

## What was not measured

The engine never started, so this test has no time to ready, no decode or prefill speed, no prefix cache result and no `/v1/messages` result. The entry stays unverified on RunPod. The earlier RunPod test on 2026-10-22 got a running pod in about 20 seconds on an RTX 2000 Ada.

## Retry

1. `ablit plan --backends runpod` chose the A40 again. `--trust verified` kept the A40, because every RunPod offer it keeps is Secure Cloud. `--min-reliability` removed every offer, because RunPod lists no reliability. The A40 and the RTX A6000 cost the same, so `--max-price-hr` cannot separate them, and ablit has no flag that picks a GPU model. The retry rented the A40 again with the same $3 cap and `--another`, because Vast and Lium runs of this entry were active.
2. RunPod again kept the pod in `starting` for the full 20 minutes with no direct SSH address. ablit destroyed the run, exited with code 6 and confirmed that billing stopped.
3. `ablit down` on the run exited 0, and `ablit status` showed it as `billing_stopped_confirmed`.
4. ablit estimated the spend at $0.20, so both attempts together came to about $0.40.

ablit reports `starting` both while RunPod says the pod is provisioning or starting and while RunPod says it is running with no direct SSH address. Its log does not say which of these happened, so this record cannot tell a pod that never started from a running pod with no public SSH port. Both attempts failed the same way on the A40, so the next step is to find out which it was before renting again.
