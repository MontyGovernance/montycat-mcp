"""Ranking-mode plumbing for `montycat_semantic_search`.

The engine gained BM25 keyword and hybrid (RRF) ranking in 1.3.4, and the client
exposes all three through one `search_values`. These assert what the tool hands
the client, and that each mode's score floor is policed on its own scale — one
shared [-1, 1] bound would reject a legitimate BM25 floor.
"""

from __future__ import annotations

import pytest

from montycat import SearchMode


class FakeKeyspace:
    """Records the single `search_values` call the tool makes."""

    def __init__(self):
        self.call = None

    async def search_values(self, **kwargs):
        self.call = kwargs
        return {"status": True, "payload": [], "error": None}


@pytest.fixture
def keyspace(server, monkeypatch):
    fake = FakeKeyspace()

    async def _bind(*_args, **_kwargs):
        return fake

    monkeypatch.setattr(server, "_bind", _bind)
    return fake


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode, expected",
    [
        ("semantic", SearchMode.SEMANTIC),
        ("keyword", SearchMode.KEYWORD),
        ("hybrid", SearchMode.HYBRID),
    ],
)
async def test_mode_reaches_the_client_as_a_search_mode(server, keyspace, mode, expected):
    await server.montycat_semantic_search(query="index decision", keyspace="k", mode=mode)

    assert keyspace.call["mode"] is expected
    assert keyspace.call["query"] == "index decision"


@pytest.mark.asyncio
async def test_search_defaults_to_semantic(server, keyspace):
    await server.montycat_semantic_search(query="anything", keyspace="k")

    assert keyspace.call["mode"] is SearchMode.SEMANTIC


@pytest.mark.asyncio
async def test_unknown_mode_is_rejected_before_the_engine(server, keyspace):
    with pytest.raises(ValueError, match="semantic, keyword, hybrid"):
        await server.montycat_semantic_search(query="anything", keyspace="k", mode="bm25")

    assert keyspace.call is None


@pytest.mark.asyncio
async def test_filters_and_time_bounds_travel_as_one_filter_map(server, keyspace):
    await server.montycat_semantic_search(
        query="index decision",
        keyspace="k",
        mode="hybrid",
        filters={"project": "montycat"},
        since="2026-01-01T00:00:00",
    )

    filters = keyspace.call["filters"]
    assert filters["project"] == "montycat"
    assert "_created_at" in filters


@pytest.mark.asyncio
async def test_unfiltered_search_sends_no_filter_map(server, keyspace):
    """`filters={}` and `filters=None` must not reach the engine differently."""
    await server.montycat_semantic_search(query="anything", keyspace="k", filters={})

    assert keyspace.call["filters"] is None


@pytest.mark.asyncio
async def test_bm25_floor_above_one_is_allowed_only_in_keyword_mode(server, keyspace):
    # Unbounded BM25: 8.0 is an ordinary keyword floor...
    await server.montycat_semantic_search(
        query="ENOSPC", keyspace="k", mode="keyword", min_score=8.0
    )
    assert keyspace.call["min_score"] == 8.0

    # ...and meaningless on the cosine and RRF scales.
    with pytest.raises(ValueError, match="between -1 and 1 in semantic mode"):
        await server.montycat_semantic_search(query="q", keyspace="k", min_score=8.0)
    with pytest.raises(ValueError, match="between 0 and 1 in hybrid mode"):
        await server.montycat_semantic_search(
            query="q", keyspace="k", mode="hybrid", min_score=8.0
        )


@pytest.mark.asyncio
async def test_negative_floor_is_rejected_off_the_cosine_scale(server, keyspace):
    """Only cosine similarity goes negative; BM25 and RRF start at 0."""
    await server.montycat_semantic_search(query="q", keyspace="k", min_score=-0.2)
    assert keyspace.call["min_score"] == -0.2

    for mode in ("keyword", "hybrid"):
        with pytest.raises(ValueError, match="at least 0|between 0 and 1"):
            await server.montycat_semantic_search(
                query="q", keyspace="k", mode=mode, min_score=-0.2
            )


@pytest.mark.asyncio
async def test_keyword_mode_needs_query_text(server, keyspace):
    """A bare vector has nothing for BM25 to score, so fail with context here."""
    with pytest.raises(ValueError, match="keyword mode needs query text"):
        await server.montycat_semantic_search(
            query="", keyspace="k", mode="keyword", vector=[0.1, 0.2]
        )

    assert keyspace.call is None

    # The vector-only path stays open for the modes that have a vector half.
    await server.montycat_semantic_search(query="", keyspace="k", vector=[0.1, 0.2])
    assert keyspace.call["vector"] == [0.1, 0.2]


class FakePersistent:
    """Stands in for a persistent keyspace class: records the `get_keys` call.

    The server binds a keyspace *class* and branches on
    `issubclass(ks, Keyspace.Persistent)`, so this is a class with classmethods,
    and the fixture points `Keyspace.Persistent` at it.
    """

    call: dict | None = None
    keys = ["3", "2", "1"]

    @classmethod
    async def get_keys(cls, **kwargs):
        cls.call = kwargs
        return {"status": True, "payload": cls.keys, "error": None}

    @classmethod
    async def get_bulk(cls, **kwargs):
        return {"status": True, "payload": kwargs["bulk_keys"], "error": None}


@pytest.fixture
def persistent(server, monkeypatch):
    FakePersistent.call = None

    async def _bind(*_args, **_kwargs):
        return FakePersistent

    monkeypatch.setattr(server, "_bind", _bind)
    monkeypatch.setattr(server.Keyspace, "Persistent", FakePersistent)
    return FakePersistent


@pytest.mark.asyncio
async def test_recent_listing_reads_a_descending_key_range(server, persistent):
    """Recency was a volume heuristic; engine >= 1.3.1 orders key ranges, so
    the newest records come back in write order rather than approximately."""
    from montycat import ResultOrder

    await server.montycat_list_memories(keyspace="k", limit=3)

    assert persistent.call["order"] is ResultOrder.DESCENDING
    assert persistent.call["limit"] == [0, 3]
    assert "latest_volume" not in persistent.call


@pytest.mark.asyncio
async def test_oldest_first_listing_reads_ascending(server, persistent):
    from montycat import ResultOrder

    await server.montycat_list_memories(keyspace="k", limit=3, recent=False)

    assert persistent.call["order"] is ResultOrder.ASCENDING


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["semantic", "keyword", "hybrid"])
@pytest.mark.parametrize("bounds, expected", [
    ({"since": "2026-01-01T00:00:00"}, {"after_timestamp": "2026-01-01T00:00:00"}),
    ({"until": "2026-02-01T00:00:00"}, {"before_timestamp": "2026-02-01T00:00:00"}),
    ({"since": "2026-01-01T00:00:00", "until": "2026-02-01T00:00:00"},
     {"range_timestamp": ["2026-01-01T00:00:00", "2026-02-01T00:00:00"]}),
])
async def test_native_timestamp_targets_selected_index(server, keyspace, mode, bounds, expected):
    from montycat import Timestamp
    from montycat.store_functions.store_generic_functions import handle_timestamps_and_pointers
    original = {"project": "montycat"}
    await server.montycat_semantic_search(
        query="index", keyspace="k", mode=mode, filters=original,
        timestamp_field="event_time", **bounds)
    sent = keyspace.call["filters"]
    assert isinstance(sent["event_time"], Timestamp)
    assert sent["event_time"].serialize() == expected
    assert handle_timestamps_and_pointers(sent) == {"event_time": expected, "project": "montycat"}
    assert original == {"project": "montycat"}
    assert "_created_at" not in sent


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs, message", [
    ({"timestamp_field": " "}, "timestamp_field"),
    ({"since": ""}, "since"),
    ({"until": " "}, "until"),
    ({"timestamp_field": "event_time", "since": "2026-01-01",
      "filters": {"event_time": "existing"}}, "already contains"),
])
async def test_invalid_time_filters_do_not_search(server, keyspace, kwargs, message):
    with pytest.raises(ValueError, match=message):
        await server.montycat_semantic_search(query="index", keyspace="k", **kwargs)
    assert keyspace.call is None
