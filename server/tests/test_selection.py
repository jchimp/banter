"""select_next() — required unit coverage (M2 step 5, PRD 3.3 / FR-13 / FR-14).

Pure function: no DB, no TestClient, no fixtures beyond local helpers. Every test
uses a seeded `random.Random` and a fixed `now` so results are deterministic.
"""

import random
from collections import Counter
from datetime import UTC, datetime, timedelta

from app.selection import Candidate, DeviceHistory, SelectionConfig, select_next

NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=UTC)
DEFAULT_CFG = SelectionConfig()


def _c(
    id: str = "rec1",
    source: str = "kid",
    *,
    created_at: datetime = NOW,
    last_played_at: datetime | None = None,
    play_count: int = 0,
    deleted: bool = False,
) -> Candidate:
    """Build a Candidate with sensible defaults so tests read as data."""
    return Candidate(
        id=id,
        source=source,
        created_at=created_at,
        last_played_at=last_played_at,
        play_count=play_count,
        deleted=deleted,
    )


NO_HISTORY = DeviceHistory(last_recording_id=None)


# --- tier 1: never-played parent recordings -----------------------------------


def test_tier1_never_played_parent_beats_everything() -> None:
    unplayed_parent = _c("parent-new", "mom", last_played_at=None)
    played_parent = _c("parent-old", "dad", last_played_at=NOW - timedelta(days=5))
    kid = _c("kid1", "kid", last_played_at=None)
    result = select_next(
        [played_parent, kid, unplayed_parent], NO_HISTORY, NOW, DEFAULT_CFG, random.Random(1)
    )
    assert result is unplayed_parent


# --- tier 1 exhaustion -> tier 2 -----------------------------------------------


def test_tier1_exhausted_falls_to_tier2_outside_cooldown() -> None:
    outside = _c("parent-outside", "mom", last_played_at=NOW - timedelta(hours=13))
    inside = _c("parent-inside", "dad", last_played_at=NOW - timedelta(hours=1))
    result = select_next([outside, inside], NO_HISTORY, NOW, DEFAULT_CFG, random.Random(2))
    assert result is outside


def test_tier2_clip_inside_cooldown_never_chosen() -> None:
    inside = _c("parent-inside", "mom", last_played_at=NOW - timedelta(hours=1))
    kid = _c("kid1", "kid", last_played_at=None)
    for seed in range(20):
        result = select_next([inside, kid], NO_HISTORY, NOW, DEFAULT_CFG, random.Random(seed))
        assert result is not inside


# --- tier 2 cooldown boundary ---------------------------------------------------


def test_tier2_boundary_exactly_at_cooldown_is_excluded() -> None:
    # cutoff = now - 12h; comparison is strictly `<`, so exactly-at-cutoff is not < cutoff.
    # A kid clip is in the pool so the assertion actually discriminates: were `at_edge`
    # tier-2 eligible it would win outright, and tier 3 would never be reached.
    at_edge = _c("parent-edge", "mom", last_played_at=NOW - timedelta(hours=12))
    kid = _c("kid1", "kid", last_played_at=None)
    for seed in range(20):
        result = select_next([at_edge, kid], NO_HISTORY, NOW, DEFAULT_CFG, random.Random(seed))
        assert result is kid  # at_edge fell past tier 2; tier 3 wins


def test_tier2_boundary_just_inside_cooldown_is_included() -> None:
    just_outside = _c(
        "parent-just-outside", "mom", last_played_at=NOW - timedelta(hours=12, seconds=1)
    )
    at_edge = _c("parent-edge", "dad", last_played_at=NOW - timedelta(hours=12))
    result = select_next([just_outside, at_edge], NO_HISTORY, NOW, DEFAULT_CFG, random.Random(4))
    assert result is just_outside


# --- tier 3: kid recordings, weighted toward staleness --------------------------


def test_tier3_chosen_when_no_eligible_parent_clips() -> None:
    inside_cooldown_parent = _c("parent-inside", "mom", last_played_at=NOW - timedelta(hours=1))
    kid = _c("kid1", "kid", last_played_at=NOW - timedelta(days=1))
    result = select_next(
        [inside_cooldown_parent, kid], NO_HISTORY, NOW, DEFAULT_CFG, random.Random(5)
    )
    assert result is kid


def test_tier3_least_recently_played_kid_favored_over_most_recent() -> None:
    stale = _c("kid-stale", "kid", last_played_at=NOW - timedelta(days=30))
    fresh = _c("kid-fresh", "kid", last_played_at=NOW - timedelta(minutes=1))
    counts: Counter[str] = Counter()
    for seed in range(2000):
        result = select_next([stale, fresh], NO_HISTORY, NOW, DEFAULT_CFG, random.Random(seed))
        assert result is not None
        counts[result.id] += 1
    # Weights are N..1 (2, 1) so stale should win roughly 2x as often as fresh.
    assert counts["kid-stale"] > counts["kid-fresh"] * 1.5


def test_tier3_never_played_kid_ranks_maximally_stale() -> None:
    never_played = _c("kid-never", "kid", last_played_at=None)
    old = _c("kid-old", "kid", last_played_at=NOW - timedelta(days=365))
    counts: Counter[str] = Counter()
    for seed in range(2000):
        result = select_next([never_played, old], NO_HISTORY, NOW, DEFAULT_CFG, random.Random(seed))
        assert result is not None
        counts[result.id] += 1
    assert counts["kid-never"] > counts["kid-old"] * 1.5


# --- tier 4: last resort ---------------------------------------------------------


def test_tier4_returns_something_when_tiers_1_to_3_empty() -> None:
    # Only a parent clip still inside its cooldown window remains.
    only = _c("parent-only", "mom", last_played_at=NOW - timedelta(minutes=5))
    result = select_next([only], NO_HISTORY, NOW, DEFAULT_CFG, random.Random(6))
    assert result is only


# --- strict tier ordering ---------------------------------------------------------


def test_strict_tier_ordering_across_seeds() -> None:
    tier1 = _c("t1", "mom", last_played_at=None)
    tier2 = _c("t2", "dad", last_played_at=NOW - timedelta(hours=13))
    tier3 = _c("t3", "kid", last_played_at=None)
    tier4 = _c("t4", "mom", last_played_at=NOW - timedelta(hours=1))
    pool = [tier4, tier3, tier2, tier1]
    for seed in range(30):
        result = select_next(pool, NO_HISTORY, NOW, DEFAULT_CFG, random.Random(seed))
        assert result is tier1


# --- no-repeat rule (FR-13) -------------------------------------------------------


def test_no_repeat_excludes_last_played_when_alternative_exists() -> None:
    # FR-13: exclude the immediately previous recording when another candidate exists.
    prev = _c("prev", "mom", last_played_at=None)
    other = _c("other", "mom", last_played_at=None)
    history = DeviceHistory(last_recording_id="prev")
    for seed in range(20):
        result = select_next([prev, other], history, NOW, DEFAULT_CFG, random.Random(seed))
        assert result is other


def test_no_repeat_falls_through_tier_when_exclusion_empties_it() -> None:
    # Tier 1 has only the previous recording; exclusion empties it, so tier 2 is used.
    prev_tier1 = _c("prev", "mom", last_played_at=None)
    tier2_alt = _c("alt", "dad", last_played_at=NOW - timedelta(hours=13))
    history = DeviceHistory(last_recording_id="prev")
    result = select_next([prev_tier1, tier2_alt], history, NOW, DEFAULT_CFG, random.Random(7))
    assert result is tier2_alt


# --- single-item exception (FR-13) -------------------------------------------------


def test_single_remaining_candidate_is_previous_recording_anyway() -> None:
    only = _c("prev", "mom", last_played_at=None)
    history = DeviceHistory(last_recording_id="prev")
    result = select_next([only], history, NOW, DEFAULT_CFG, random.Random(8))
    assert result is only


# --- avoid_immediate_repeat=False --------------------------------------------------


def test_avoid_immediate_repeat_false_disables_exclusion() -> None:
    prev = _c("prev", "mom", last_played_at=None)
    other = _c("other", "mom", last_played_at=None)
    history = DeviceHistory(last_recording_id="prev")
    cfg = SelectionConfig(avoid_immediate_repeat=False)
    seen_prev = False
    for seed in range(30):
        result = select_next([prev, other], history, NOW, cfg, random.Random(seed))
        assert result in (prev, other)
        if result is prev:
            seen_prev = True
    assert seen_prev


# --- deleted candidates excluded ---------------------------------------------------


def test_deleted_excluded_from_tier1() -> None:
    deleted_parent = _c("deleted-parent", "mom", last_played_at=None, deleted=True)
    live_kid = _c("kid1", "kid", last_played_at=None)
    result = select_next([deleted_parent, live_kid], NO_HISTORY, NOW, DEFAULT_CFG, random.Random(9))
    assert result is live_kid


def test_deleted_excluded_from_tier4_last_resort() -> None:
    deleted_only = _c("deleted-only", "mom", last_played_at=None, deleted=True)
    result = select_next([deleted_only], NO_HISTORY, NOW, DEFAULT_CFG, random.Random(10))
    assert result is None


# --- empty pool ---------------------------------------------------------------------


def test_empty_candidate_list_returns_none() -> None:
    assert select_next([], NO_HISTORY, NOW, DEFAULT_CFG, random.Random(11)) is None


def test_all_deleted_returns_none() -> None:
    all_deleted = [
        _c("a", "mom", deleted=True),
        _c("b", "kid", deleted=True),
    ]
    assert select_next(all_deleted, NO_HISTORY, NOW, DEFAULT_CFG, random.Random(12)) is None


# --- purity -------------------------------------------------------------------------


def test_select_next_does_not_mutate_input_sequence() -> None:
    candidates = [
        _c("a", "mom", last_played_at=None),
        _c("b", "kid", last_played_at=NOW - timedelta(days=1)),
        _c("c", "dad", last_played_at=NOW - timedelta(hours=13), deleted=True),
    ]
    before = list(candidates)
    select_next(candidates, NO_HISTORY, NOW, DEFAULT_CFG, random.Random(13))
    assert candidates == before
