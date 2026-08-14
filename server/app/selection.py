"""Pure selection logic for `GET /api/recordings/next` (PRD 3.3 / FR-14).

`select_next` takes data in, returns a choice, and does no I/O: no DB calls, no
clock reads, no module-level RNG. Callers (the API route) are responsible for
building `Candidate`/`DeviceHistory` from the DB and passing `datetime.now(UTC)`
in explicitly. This is what makes the tier rules unit-testable without a DB or
mocked clock.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

_PARENT_SOURCES = frozenset({"mom", "dad"})


@dataclass(frozen=True)
class Candidate:
    """A recording as seen by the selection algorithm.

    `last_played_at` is device-scoped: the caller resolves it from the `plays`
    table for the requesting device, not from `recordings.last_played_at`.
    """

    id: str
    source: str  # 'kid' | 'mom' | 'dad'
    created_at: datetime  # tz-aware UTC
    last_played_at: datetime | None  # None = never played on this device
    play_count: int = 0
    deleted: bool = False


@dataclass(frozen=True)
class DeviceHistory:
    """What a device played most recently, for the no-repeat rule (FR-13)."""

    last_recording_id: str | None = None


@dataclass(frozen=True)
class SelectionConfig:
    """Tunables for tier 2 and the no-repeat rule."""

    parent_cooldown_hours: int = 12
    avoid_immediate_repeat: bool = True


def select_next(
    candidates: Sequence[Candidate],
    history: DeviceHistory,
    now: datetime,
    cfg: SelectionConfig,
    rng: random.Random | None = None,
) -> Candidate | None:
    """Pick the next recording to play, per the PRD 3.3 tier table.

    Tiers, in strict order (first non-empty tier wins):
      1. Parent recordings never played on this device.
      2. Parent recordings last played more than `cfg.parent_cooldown_hours`
         ago on this device.
      3. Kid recordings, weighted toward least-recently-played (see
         `_pick_weighted` for the exact formula).
      4. Any remaining non-deleted recording.

    No-repeat rule (FR-13): when `cfg.avoid_immediate_repeat` is set,
    `history.last_recording_id` is excluded from each tier's pool before it's
    considered. If a tier is empty only because of that exclusion, selection
    falls through to the next tier rather than returning the repeat early.
    If tier 4 is also exhausted this way — i.e. the previous recording is the
    only non-deleted candidate left — it's returned anyway, since there is
    nothing else to offer.

    Args:
        candidates: All recordings visible to the device, deleted or not.
        history: The device's play history (currently just the last play).
        now: Caller-supplied current time (tz-aware UTC). Never read from the
            clock inside this function.
        cfg: Cooldown and no-repeat settings.
        rng: Source of randomness. A fresh `random.Random()` is constructed
            when omitted; pass a seeded instance for deterministic tests.

    Returns:
        The chosen `Candidate`, or `None` if there is nothing playable
        (empty input, or everything deleted).
    """
    if rng is None:
        rng = random.Random()

    live = [c for c in candidates if not c.deleted]
    if not live:
        return None

    exclude_id = history.last_recording_id if cfg.avoid_immediate_repeat else None

    for pool in (_tier_one(live), _tier_two(live, now, cfg)):
        chosen = _pick_uniform(pool, exclude_id, rng)
        if chosen is not None:
            return chosen

    chosen = _pick_weighted(_tier_three(live), exclude_id, rng)
    if chosen is not None:
        return chosen

    chosen = _pick_uniform(live, exclude_id, rng)
    if chosen is not None:
        return chosen

    # FR-13 exception: the only non-deleted candidate left is the immediately
    # previous recording. There's nothing else to play, so replay it.
    if exclude_id is not None and len(live) == 1:
        return live[0]
    return None


def _tier_one(live: Sequence[Candidate]) -> list[Candidate]:
    """Parent recordings never played on this device."""
    return [c for c in live if c.source in _PARENT_SOURCES and c.last_played_at is None]


def _tier_two(live: Sequence[Candidate], now: datetime, cfg: SelectionConfig) -> list[Candidate]:
    """Parent recordings last played outside the cooldown window."""
    cutoff = now - timedelta(hours=cfg.parent_cooldown_hours)
    return [
        c
        for c in live
        if c.source in _PARENT_SOURCES
        and c.last_played_at is not None
        and c.last_played_at < cutoff
    ]


def _tier_three(live: Sequence[Candidate]) -> list[Candidate]:
    """Kid recordings, the pool `_pick_weighted` ranks by staleness."""
    return [c for c in live if c.source == "kid"]


def _pick_uniform(
    pool: Sequence[Candidate], exclude_id: str | None, rng: random.Random
) -> Candidate | None:
    """Uniform random pick from `pool`, excluding `exclude_id` when possible.

    Returns None if the pool is empty outright, or empty only because the
    single excluded id was removed — either way the caller falls through to
    the next tier (or, for the last tier, applies the FR-13 exception).
    """
    filtered = pool if exclude_id is None else [c for c in pool if c.id != exclude_id]
    return rng.choice(filtered) if filtered else None


def _pick_weighted(
    pool: Sequence[Candidate], exclude_id: str | None, rng: random.Random
) -> Candidate | None:
    """Weighted pick favoring least-recently-played, per FR-13/tier 3.

    Rank the pool by staleness: `last_played_at` ascending, `None` (never
    played) sorts first as maximally stale; ties break by `id` ascending so
    the ranking is total and deterministic. For a pool of size N, the item at
    rank `i` (0-based, 0 = stalest) is weighted `N - i`, so weights run
    N..1 and are never zero. `rng.choices` then draws one item using those
    weights.
    """
    filtered = pool if exclude_id is None else [c for c in pool if c.id != exclude_id]
    if not filtered:
        return None
    ranked = sorted(
        filtered,
        key=lambda c: (c.last_played_at is not None, c.last_played_at, c.id),
    )
    n = len(ranked)
    weights = [n - i for i in range(n)]
    return rng.choices(ranked, weights=weights, k=1)[0]
