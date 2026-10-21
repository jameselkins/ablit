# Boot test: ds41-september-gsq-uncensored on Vast, 2026-10-21

The maintainer ran this test with `ablit up` on their own Vast account. Host IDs, offer IDs, prices and keys are left out.

## Machine

One RTX PRO 6000 Max-Q with 98 GB of VRAM on a Vast verified host, with the pinned Xolotl image (`ghcr.io/jameselkins/ablit-xolotl@sha256:5c675f44...a204`) and the profile in the catalog entry.

This machine has four times the VRAM of the entry's 24 GB floor, so the test shows the entry boots and serves. It does not test the 24 GB floor or the speed on a 24 GB card.

The container's locked-memory limit (`ulimit -l`) was 8 MiB, against the 60 GiB arena the engine asks for. Vast containers accept no `--ulimit` option. The bootstrap logged a warning and the engine started anyway.

## Stage times

Times come from the bootstrap log and the run record, in UTC.

| Stage | Time |
|---|---|
| Rental created to bootstrap start (image pull and container start) | about 3.7 minutes |
| Checks, disk read test and download speed check | under 1 minute |
| Download and verify 371.7 GB, with the weight join running alongside | about 11 minutes, 4.1 to 4.8 Gbps |
| Engram tables | read in place from the official shards, no extraction |
| Engine start, health check and ready | about 2 minutes |

The run was ready about 17 minutes after the rental was created.

## Results

1. Chat completions came back through the SSH tunnel. A 1,024-token answer took 21.9 seconds end to end, which is 46.8 tokens per second. The engine log reported 48 to 65 tokens per second while it generated, with an expert cache hit rate of 98.5 to 99.6 percent.
2. A request without the endpoint key got HTTP 401.
3. `ablit down` destroyed the machine and confirmed that billing stopped.

Two earlier attempts the same day did not reach the download. In the first, the bootstrap stopped on the locked-memory check, which is now a warning. In the second, the host stayed in Vast's loading state for 32 minutes and the run was destroyed. ablit now destroys a run that is not running 20 minutes after create.
