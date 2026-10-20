"""Run files, locks, lifecycle, the host deny list and the tunnel.

State lives in the platform's user state directory. There is no database and no
daemon.

Lifecycle, as the specification gives it:

1. Write runs/<run-id>.json with the approved quote and state `creating`,
   through a temporary file and a rename, and flush it to disk.
2. Put the run ID in the instance label. If a create request times out, list
   the user's instances and look for the label before doing anything else.
3. Record every resource ID, including volumes, as soon as it is known.
4. Use a lock file per run.
5. `down` moves through `termination_requested`, `resources_terminated` and
   `billing_stopped_confirmed`.

TODO(step 3): write all of it.
"""

RUN_STATES = (
    "creating",
    "running",
    "termination_requested",
    "resources_terminated",
    "billing_stopped_confirmed",
)
