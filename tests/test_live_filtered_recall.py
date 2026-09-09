"""Filtered recall against a live engine: meaning + metadata + time in one call.

The filter is a hard AND over indexed fields; ranking stays pure cosine. These
assert the payload's *content*, not just a non-error status — a server that
ignored `semantic_filter` would return status true with unfiltered hits, which
only a content check catches.

Ranking modes (keyword and hybrid BM25/RRF) live in `test_live_search_modes.py`;
"hybrid" here would mean the older sense of the word.
"""

from __future__ import annotations

import asyncio

import pytest

from .conftest import requires_engine

pytestmark = [pytest.mark.asyncio, requires_engine]

SPACE_QUERY = "astronomy and outer space"

CORPUS = [
    ("we chose usearch HNSW for the vector index", "montycat", None),
    ("sourdough needs a wild yeast starter", "cooking", None),
    ("old decision: sled as the persistent backend", "montycat", "2020-01-01T00:00:00"),
]


def payload(result):
    """Hits from a successful call; an engine failure raises rather than reads
    as "no results" — a masked error is a 90-second lie about embeddings."""
    if isinstance(result, dict) and result.get("status") is False:
        pytest.fail(f"engine call failed: {result.get('error')}")
    body = result.get("payload") if isinstance(result, dict) else None
    return body if isinstance(body, list) else []


async def seed(server, ks):
    for text, project, created in CORPUS:
        value = {"text": text, "project": project}
        if created:
            value["_created_at"] = created
        await server.montycat_remember(value=value, keyspace=ks)

    # Embedding is a background batch job. Wait for ALL of them — stopping at the
    # first hit would race every count assertion below.
    for _ in range(90):
        hits = payload(await server.montycat_semantic_search(
            query="vector index choice", keyspace=ks, limit=10))
        if len(hits) >= len(CORPUS):
            return hits
        await asyncio.sleep(1)
    pytest.fail("embeddings did not converge within 90s")


async def test_created_at_is_stamped_and_readable(server, keyspace):
    await seed(server, keyspace)
    result = await server.montycat_semantic_search(
        query="vector index choice", keyspace=keyspace, limit=10)
    for hit in payload(result):
        assert "_created_at" in hit["__value__"]


async def test_metadata_filter_restricts_and_preserves_ranking(server, keyspace):
    await seed(server, keyspace)
    # Baseline must use the SAME query as the filtered call, or the scores are
    # simply from a different search and prove nothing.
    unfiltered = payload(await server.montycat_semantic_search(
        query=SPACE_QUERY, keyspace=keyspace, limit=10))
    scores = {h["__key__"]: h["__score__"] for h in unfiltered}

    hits = payload(await server.montycat_semantic_search(
        query=SPACE_QUERY, keyspace=keyspace, filters={"project": "montycat"}))

    assert len(hits) == 2
    assert all(h["__value__"]["project"] == "montycat" for h in hits)
    # The filter constrains; it must never rescore.
    assert all(h["__score__"] == scores[h["__key__"]] for h in hits)
    # And it preserves relative order.
    keys = [h["__key__"] for h in hits]
    assert keys == [h["__key__"] for h in unfiltered if h["__key__"] in set(keys)]


async def test_filter_beats_query_topic(server, keyspace):
    """Query says space, filter says cooking — the filter wins, and the hits
    score below every space hit."""
    await seed(server, keyspace)
    space = payload(await server.montycat_semantic_search(
        query=SPACE_QUERY, keyspace=keyspace, filters={"project": "montycat"}))
    cooking = payload(await server.montycat_semantic_search(
        query=SPACE_QUERY, keyspace=keyspace, filters={"project": "cooking"}))

    assert all(h["__value__"]["project"] == "cooking" for h in cooking)
    assert max(h["__score__"] for h in cooking) < min(h["__score__"] for h in space)


async def test_unmatched_filter_returns_empty(server, keyspace):
    """Also the canary for a server that ignores `semantic_filter` entirely:
    it would return the full unfiltered top-k here."""
    await seed(server, keyspace)
    hits = payload(await server.montycat_semantic_search(
        query=SPACE_QUERY, keyspace=keyspace, filters={"project": "nope"}))
    assert hits == []


async def test_since_and_until_split_the_corpus(server, keyspace):
    await seed(server, keyspace)

    recent = payload(await server.montycat_semantic_search(
        query=SPACE_QUERY, keyspace=keyspace, limit=10, since="2025-01-01T00:00:00"))
    assert len(recent) == 2, "the 2020 memory must be excluded"

    old = payload(await server.montycat_semantic_search(
        query=SPACE_QUERY, keyspace=keyspace, limit=10, until="2021-01-01T00:00:00"))
    assert len(old) == 1
    assert old[0]["__value__"]["_created_at"] == "2020-01-01T00:00:00"


async def test_time_bounds_target_selected_timestamp_index(server, keyspace):
    values = [
        {
            "text": "old release used sled storage",
            "timestamps": {"event_time": "2020-01-01T00:00:00"},
        },
        {
            "text": "new release uses updated storage",
            "timestamps": {"event_time": "2026-01-01T00:00:00"},
        },
    ]
    for value in values:
        result = await server.montycat_remember(
            value=value, keyspace=keyspace, wait_for_index=True
        )
        assert result["status"], result

    old = payload(await server.montycat_semantic_search(
        query="release storage", keyspace=keyspace, limit=10,
        timestamp_field="event_time", until="2021-01-01T00:00:00"))
    assert len(old) == 1
    assert old[0]["__value__"]["event_time"] == "2020-01-01T00:00:00"

    new = payload(await server.montycat_semantic_search(
        query="release storage", keyspace=keyspace, limit=10,
        timestamp_field="event_time", since="2025-01-01T00:00:00"))
    assert len(new) == 1
    assert new[0]["__value__"]["event_time"] == "2026-01-01T00:00:00"


async def test_time_window_and_metadata_compose(server, keyspace):
    """The whole point of filtered recall: both constraints in one call."""
    await seed(server, keyspace)
    hits = payload(await server.montycat_semantic_search(
        query=SPACE_QUERY, keyspace=keyspace, limit=10,
        since="2025-01-01T00:00:00", filters={"project": "montycat"}))
    assert len(hits) == 1
    assert hits[0]["__value__"]["project"] == "montycat"
    assert hits[0]["__value__"]["_created_at"] != "2020-01-01T00:00:00"


async def test_min_score_cut_drops_the_weaker_hit(server, keyspace):
    await seed(server, keyspace)
    space = payload(await server.montycat_semantic_search(
        query=SPACE_QUERY, keyspace=keyspace, filters={"project": "montycat"}))
    scores = sorted((h["__score__"] for h in space), reverse=True)
    cut = (scores[0] + scores[1]) / 2

    hits = payload(await server.montycat_semantic_search(
        query=SPACE_QUERY, keyspace=keyspace,
        filters={"project": "montycat"}, min_score=cut))
    assert len(hits) == 1
    assert hits[0]["__score__"] == scores[0]


async def test_score_is_a_number_not_a_string(server, keyspace):
    """The engine rounds `__score__` to f32-honest precision precisely so
    clients that stringify wide numbers (Node's json-bigint) don't hand back a
    string that breaks arithmetic."""
    await seed(server, keyspace)
    hits = payload(await server.montycat_semantic_search(
        query=SPACE_QUERY, keyspace=keyspace, limit=5))
    assert all(isinstance(h["__score__"], (int, float)) for h in hits)


async def test_empty_filters_means_unfiltered(server, keyspace):
    """`filters={}` is "no constraint", not "match nothing" — it must behave
    exactly like omitting the argument."""
    await seed(server, keyspace)
    with_empty = payload(await server.montycat_semantic_search(
        query=SPACE_QUERY, keyspace=keyspace, limit=10, filters={}))
    without = payload(await server.montycat_semantic_search(
        query=SPACE_QUERY, keyspace=keyspace, limit=10))
    assert [h["__key__"] for h in with_empty] == [h["__key__"] for h in without]


async def test_empty_query_is_rejected(server, keyspace):
    with pytest.raises(ValueError):
        await server.montycat_semantic_search(query="   ", keyspace=keyspace)
