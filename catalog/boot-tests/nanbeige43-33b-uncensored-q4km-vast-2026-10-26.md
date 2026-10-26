# Boot test: nanbeige43-33b-uncensored-q4km on Vast, 2026-10-26

The maintainer ran this test with `ablit up --backends vast --untested --max-spend 3` on their own Vast account. Instance IDs, IP addresses and keys are left out.

## Machine

One Tesla V100 with 32 GB of VRAM on Vast (unverified host, reliability 0.987, region US), with 48 GB of host RAM, at $0.105 an hour all in. This was the default pick from `ablit plan`. It used the pinned llama.cpp server image and the profile in the catalog entry, with a context of 32,768 tokens.

## What happened

1. Vast reported the instance as loading for about 100 seconds after create, and SSH answered about 25 seconds later.
2. ablit ran the bootstrap and downloaded the 20.1 GB GGUF file in about 4 minutes, mostly at 600 to 900 Mbps, with one short drop to 15 Mbps. It then verified the file and started llama-server.
3. The endpoint answered through the SSH tunnel about 9 minutes and 10 seconds after the rental started.
4. Decode speed: three chat completions with a short prompt, `max_tokens` 1024 and temperature 0 each produced 1,024 tokens. llama.cpp reported 31.8 tokens per second each time. Wall times were 42.3, 34.2 and 33.8 seconds, which is 24.2, 30.0 and 30.3 tokens per second end to end. The first request was slower because the engine was still warming up. The reply's `content` field was empty because the model spent all 1,024 tokens thinking.
5. Prefill speed: a 20,658-token prompt with `max_tokens` 64 prefilled at 503 tokens per second (41.1 seconds), and the whole request took 50.8 seconds. Decode on that long context was 21.4 tokens per second.
6. Prefix cache: the same prompt plus one extra sentence reused 20,674 cached tokens and prefilled only 20 new ones in 0.19 seconds. The whole request took 10.1 seconds instead of 50.8.
7. `/v1/messages`: the Anthropic Messages endpoint exists. A request with one `get_weather` tool returned a `thinking` block, then a `tool_use` block with input `{"city":"Paris"}` and `stop_reason` `tool_use`. This worked with streaming on and off, and with both `Authorization: Bearer` and `x-api-key`. Streaming returned the standard event types, and the tool input arrived as `input_json_delta`. A request with no key got a 401.
8. `ablit down` destroyed the instance and confirmed that billing stopped on the first try (exit 0). Total estimated spend was $0.18 for about 20 minutes, including the model download.

## Second machine: RTX 5090

One RTX 5090 with 32.6 GB of VRAM on Vast (verified host, reliability 0.994, region CA), with 64 GB of host RAM, at $0.579 an hour all in. The run used `--trust verified --region CA --max-spend 2.8`. `--trust verified` alone ranked a CMP 170HX first at that moment, and `up` has no flag that picks a GPU model, so `--region CA` was added to make the RTX 5090 the chosen offer. The lower cap kept the two runs together under $3.

1. Vast reported the instance as loading for about 70 seconds after create, and SSH answered about 30 seconds later.
2. The 20.1 GB GGUF file downloaded in about 3 minutes, mostly at 600 to 940 Mbps.
3. The endpoint answered through the SSH tunnel about 5 minutes after the rental started.
4. Decode speed: three chat completions with the same short prompt each produced 1,024 tokens. llama.cpp reported 73.6, 73.6 and 73.5 tokens per second. Wall times were 14.5, 14.7 and 14.7 seconds, which is 70.6, 69.9 and 69.9 tokens per second end to end. The `content` field was empty again because the model spent all 1,024 tokens thinking.
5. Prefill speed: the same 20,658-token prompt prefilled at 2,923 tokens per second (7.1 seconds), and the whole request took 9.2 seconds. Decode on that long context was 61.3 tokens per second.
6. Prefix cache: the same prompt plus one extra sentence reused 20,674 cached tokens and prefilled 20 new ones in 0.03 seconds. The first streamed byte arrived after 1.0 second, and the whole request took 2.0 seconds instead of 9.2.
7. `/v1/messages`: the results matched the first machine. A `thinking` block and then a `tool_use` block with input `{"city":"Paris"}` came back, with `stop_reason` `tool_use`. This worked with streaming on and off, and with both `Authorization: Bearer` and `x-api-key`. A request with no key got a 401.
8. `ablit down` destroyed the instance and confirmed that billing stopped on the first try (exit 0). Total estimated spend was $0.06 for about 7 minutes.

Both runs together cost about $0.24.

## Notes

Two other boot tests of this entry, on RunPod and Lium, ran at the same time from the same computer. The first `up` exited 2 with "Run selection is busy in another command", because another `up` held the selection lock. A retry a few seconds later went through, and it needed `--another` because the other runs of the same model were active.
