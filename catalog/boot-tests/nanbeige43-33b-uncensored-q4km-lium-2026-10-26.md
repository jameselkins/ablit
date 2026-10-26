# Boot test: nanbeige43-33b-uncensored-q4km on Lium, 2026-10-26

The maintainer ran this test with `ablit up --backends lium --untested` on their own Lium account. It covers two runs, one after the other. Pod IDs, IP addresses and keys are left out.

## Machines

1. Run 1 used the default offer: one L40 (Lium reports 45 GB VRAM), 71 GB host RAM, verified tier, region US, $0.33 an hour.
2. Run 2 used `--region KZ` to pick an RTX 5090: one RTX 5090 (32 GB VRAM), 110 GB host RAM, verified tier, region KZ, $0.85 an hour.

Both runs used the pinned llama.cpp server image and the profile in the catalog entry (Q4_K_M GGUF, 20.1 GB, context 32768).

## What happened

1. On the L40, Lium reported the pod as starting for about 90 seconds before SSH answered. The 20.1 GB download ran at 2 to 4.5 Gbps. The endpoint answered about 3 minutes after create.
2. On the RTX 5090, the pod was starting for about 3 minutes. The download ran at 700 to 900 Mbps. The endpoint answered about 7.7 minutes after create.
3. Decode speed: three chat completions with a short prompt, `max_tokens` 1024 and temperature 0, each generated 1024 tokens. On the L40, each took 30.1 to 30.2 seconds wall time, which is 34 tokens per second end to end, and llama.cpp reported 34.4 to 34.5 `predicted_per_second`. On the RTX 5090, each took 14.4 to 14.6 seconds, which is 70 to 71 tokens per second end to end, and llama.cpp reported 72.8 to 73.1.
4. Prefill speed: a 23,674-token prompt made from the repo's Python files, with `max_tokens` 64. On the L40, prefill ran at 1,303 tokens per second (18.2 seconds of prompt processing, 21.3 seconds wall time). On the RTX 5090, it ran at 2,799 tokens per second (8.5 seconds, 10.4 seconds wall time). The model streams its reasoning first, so the test did not record a separate time to first token. Prompt processing time sets most of the wait.
5. Prefix cache: the same prompt with one extra sentence reused 23,690 cached tokens and processed only 16 new ones. Wall time fell to 3.1 seconds on the L40 and 2.5 seconds on the RTX 5090, most of it spent generating the 64 tokens.
6. `/v1/messages` (Anthropic Messages format) exists and works on this image. A request with one `get_weather` tool and a prompt asking for the weather in Paris returned HTTP 200 with a `thinking` block, then a `tool_use` block calling `get_weather` with `{"city": "Paris"}`, and `stop_reason: "tool_use"`. Streaming returned the standard events (`message_start`, `content_block_start`, `content_block_delta` with `input_json_delta`, `content_block_stop`, `message_delta`, `message_stop`). Both `Authorization: Bearer` and `x-api-key` were accepted, with and without streaming, on both machines. The `thinking` block carries an empty `signature`.
7. `ablit down` deleted each pod and confirmed that billing stopped, exiting 0 on the first try. Both runs show `billing_stopped_confirmed` with nothing still billable.
8. Total estimated spend was about $0.16: about $0.03 for the L40 run (about 6 minutes) and about $0.13 for the RTX 5090 run (about 9 minutes).

## What failed first

The first `ablit up --backends lium` attempt did not rent anything. A RunPod test of the same model, started from the same directory a few seconds earlier, had a run in state `creating` with the same cap and build. Because `--another` was not passed, `up` resumed that RunPod run instead of starting a Lium one. It took the RunPod run's lock and waited on its pod. That process was killed without cleanup, and the RunPod run was left to its owner. Both runs in this record used `--another`. When more than one `up` for the same model may run at once, pass `--another`, or `up` may attach to a run started for a different backend.
