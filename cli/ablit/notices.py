"""Fixed notices that the quote, the README and `ablit up --help` all print."""

SPEND_CAP_NOTICE = """\
How the spend cap works: a watchdog on the rented machine destroys it when
spend reaches $<cap>, and ablit checks too while it is running on this computer.
If the rented machine freezes or loses its network while this computer is off
or asleep, neither check can run, and the marketplace keeps billing until you
run `ablit down` or the rental ends. Check `ablit status` if you leave a run
unattended.
"""

# Added to the spend cap notice when the chosen offer is on a marketplace that has no key limited
# to one machine. The machine then cannot be trusted to destroy itself, so the check that runs on
# this computer carries the cap. Sources: backends/runpod.md and backends/clore.md.
LOCAL_CAP_NOTES = {
    "runpod": (
        "On RunPod, ablit cannot create a key that is limited to one machine, so the machine does not "
        "get one. The cap on this machine relies on the check that runs on this computer. The machine "
        "also tries to destroy itself with the key RunPod puts in it, and that has not been tested. "
        "Keep in your RunPod account only an amount you are willing to spend."
    ),
    "clore": (
        "On Clore, a key cannot be limited to one machine, so the machine does not get one and cannot "
        "cancel its own order. The cap on this machine relies on the check that runs on this computer. "
        "If this computer is off or asleep, nothing cancels the order. Clore ends an order only when "
        "the account balance runs out. Keep in your Clore account only an amount you are willing to spend."
    ),
}

SPOT_WARNING = """\
An interruptible machine can be taken away at any time. Your endpoint will stop
without notice, downloaded files may be lost, and on Vast a paused machine's
disk is still billed. The download and setup charges count toward your cap.
"""

UNTESTED_NOTICE = (
    "Nobody has booted this entry yet. The engine may fail to start, and you "
    "pay for the time until ablit destroys the run."
)

PLAIN_HTTP_WARNING = (
    "This endpoint uses plain HTTP. Your API key and your prompts cross the "
    "internet unencrypted."
)


def spend_cap_notice(cap_usd: float | None = None) -> str:
    """Return the spend-cap notice, with the cap filled in when it is known."""
    if cap_usd is None:
        return SPEND_CAP_NOTICE
    return SPEND_CAP_NOTICE.replace("$<cap>", f"${cap_usd:.2f}")
