# RunPod driver notes

These notes describe what the RunPod driver will send and read. Nothing has been sent to RunPod yet, and the driver in `cli/ablit/backends/runpod.py` is a stub. Searching comes first, and renting follows only after a round-trip test. Each detail below is a plan to confirm against a live response.

## Search

RunPod uses GraphQL. The driver asks for GPU types with their lowest price for the requested arrangement.

```
gpuTypes {
  lowestPrice(input: {gpuCount: <n>, minMemoryInGb: <n>, ...}) {
    uninterruptablePrice
    stockStatus
    ...
  }
}
```

1. Leave `secureCloud` out of the input to see community prices as well. `--trust verified` keeps only Secure Cloud.
2. `uninterruptablePrice` is the hourly total for all GPUs, so it is not multiplied by the GPU count again.
3. An offer counts as available only when `stockStatus` is not null.
4. RunPod reports no download speed per host. The planner keeps these offers, labels them "speed not advertised, measured after boot", and ranks them as if they ran exactly at the speed floor.
5. The hourly price used for ranking is `uninterruptablePrice` for all GPUs plus the volume disk. Transfer is counted as zero.

## Renting

The driver rents with the `podFindAndDeployOnDemand` mutation and sets `terminateAfter` to the time at which the spend cap would be reached. After the pod starts, the driver compares the allocated RAM, disk and hourly rate with the quote.

The quote mentions `terminateAfter` only after a test shows that RunPod enforces it and deletes the volume. Until then the setting is a precaution that the user is not told to rely on.

## Cleanup

1. The run ID goes into the pod name. If a create request times out or its response is lost, the driver lists the user's pods and looks for the name before doing anything else.
2. Every resource ID, including volumes, is recorded as soon as it is known.
3. Destroying a run must remove the volume as well as the pod, and the driver confirms with RunPod that billing stopped before it prints "Billing stopped".

## Watchdog credential

The watchdog needs to delete its own pod. The driver tests whether the pod's own `RUNPOD_API_KEY` can delete the pod. It never puts the user's full account key on the machine. If the pod key cannot delete the pod, the watchdog stops only the engine, and the quote says that the cap on this backend relies on the local check.

## Not yet verified

- The exact field names in the GraphQL schema.
- Whether `terminateAfter` is enforced, and whether it deletes the volume.
- Whether the pod's own key can delete the pod.
- The download speed that RunPod hosts deliver.
