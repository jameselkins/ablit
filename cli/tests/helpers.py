"""Shared builders for offers and requests."""

from __future__ import annotations

from hypothesis import strategies as st

from ablit.select import HardwareRequest, Offer, PlanOptions

REQ = HardwareRequest(
    gpu_vendor="nvidia",
    gpu_count=1,
    vram_gb_each=24,
    host_ram_gb=90,
    disk_gb=600,
    download_gb=370,
    startup_hours=0.5,
    min_download_mbps=500,
)
OPTS = PlanOptions(hours=1.0)


def make_offer(offer_id: str = "o1", backend: str = "vast", hourly: float = 1.0, **overrides) -> Offer:
    base = dict(
        backend=backend,
        offer_id=offer_id,
        gpu_model="RTX 4090",
        gpu_count=1,
        gpu_vram_gb_each=24,
        hourly_all_in=hourly,
        host_ram_gb=128,
        disk_gb=1000,
        compute_capability=8.9,
        download_mbps=1000.0,
    )
    base.update(overrides)
    return Offer(**base)


BACKENDS = ["aaa-market", "runpod", "vast", "zzz-market"]


@st.composite
def offer_sets(draw, min_size: int = 0, max_size: int = 200):
    """Offer sets with unique hourly prices, mixed hardware and mixed transfer prices."""
    prices = draw(st.lists(st.integers(10, 20000), min_size=min_size, max_size=max_size, unique=True))
    offers = []
    for index, cents in enumerate(prices):
        offers.append(
            Offer(
                backend=draw(st.sampled_from(BACKENDS)),
                offer_id=f"offer-{index:04d}",
                gpu_model="GPU",
                gpu_count=draw(st.sampled_from([1, 1, 1, 2])),
                gpu_vram_gb_each=draw(st.sampled_from([16, 24, 24, 48])),
                hourly_all_in=cents / 100.0,
                host_ram_gb=draw(st.sampled_from([64, 128, 256])),
                disk_gb=draw(st.sampled_from([400, 800, 1000])),
                compute_capability=draw(st.sampled_from([None, 7.0, 8.9])),
                bandwidth_cost_per_gb=draw(st.sampled_from([0.0, 0.0, 0.001, 0.02, 0.1])),
                fixed_fees=draw(st.sampled_from([0.0, 0.0, 0.5])),
                download_mbps=draw(st.sampled_from([None, 300.0, 600.0, 1000.0, 4000.0])),
                interruptible=draw(st.booleans()),
                verified=draw(st.booleans()),
            )
        )
    return offers
