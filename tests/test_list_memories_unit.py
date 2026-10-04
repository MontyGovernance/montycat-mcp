"""Storage-type dispatch in `montycat_list_memories`.

The two keyspace clients do not share a `get_keys` signature:

    inmemory_kv.get_keys(volumes=[], latest_volume=False)
    persistent_kv.get_keys(limit=[], order=None, volumes=[], latest_volume=False)

Only the persistent one takes a range. Handing `limit=` to an in-memory
keyspace raises `TypeError: get_keys() got an unexpected keyword argument
'limit'` — a crash, not a failure envelope — on every `recent=False` call and on
the fallback that widens the scan when the latest volume is empty. These tests
pin the dispatch. No engine required: the keyspaces and engine are fakes with
the real signatures.
"""

from __future__ import annotations

import pytest
from montycat import Keyspace


def _structure(volumes):
    """An engine structure payload holding one in-memory keyspace."""
    return {
        "status": True,
        "payload": {
            "structure": {
                "a_store": {
                    "inmemory": {"vol_ks": {"index_counts": {}, "volumes": volumes}},
                    "persistent": {},
                }
            }
        },
        "error": None,
    }


class FakeEngine:
    store = "a_store"

    def __init__(self, structure):
        self._structure = structure

    async def get_structure_available(self):
        return self._structure


def _keyspace_pair(latest_keys, all_keys):
    """Build an in-memory and a persistent fake that record their get_keys call."""

    calls = []

    class InMemoryKS(Keyspace.InMemory):
        keyspace = "vol_ks"

        @classmethod
        async def get_keys(cls, volumes: list = [], latest_volume: bool = False):
            calls.append({"volumes": volumes, "latest_volume": latest_volume})
            if latest_volume:
                return {"status": True, "payload": latest_keys, "error": None}
            if not volumes:
                raise ValueError("Please provide volumes/latest volume.")
            return {"status": True, "payload": all_keys, "error": None}

        @classmethod
        async def get_bulk(cls, bulk_keys, key_included=False):
            return {"status": True, "payload": list(bulk_keys), "error": None}

    class PersistentKS(Keyspace.Persistent):
        keyspace = "range_ks"

        @classmethod
        async def get_keys(cls, limit: list = [], order=None, volumes: list = [],
                           latest_volume: bool = False):
            calls.append({"limit": limit, "volumes": volumes,
                          "latest_volume": latest_volume})
            if latest_volume:
                return {"status": True, "payload": latest_keys, "error": None}
            return {"status": True, "payload": all_keys, "error": None}

        @classmethod
        async def get_bulk(cls, bulk_keys, key_included=False):
            return {"status": True, "payload": list(bulk_keys), "error": None}

    return InMemoryKS, PersistentKS, calls


@pytest.fixture
def listing(server, monkeypatch):
    """Wire `montycat_list_memories` to fake keyspaces and a fake engine."""

    async def _ready():
        return None

    monkeypatch.setattr(server, "_engine_ready", _ready)

    def _install(ks, structure):
        async def _bind(name=None, persistent=None):
            return ks

        monkeypatch.setattr(server, "_bind", _bind)
        monkeypatch.setattr(server, "_engine", FakeEngine(structure))

    return _install


@pytest.mark.asyncio
async def test_in_memory_full_scan_names_volumes_instead_of_a_range(server, listing):
    """`recent=False` on an in-memory keyspace used to raise TypeError."""
    inmem, _persistent, calls = _keyspace_pair([], ["k1", "k2"])
    listing(inmem, _structure({"0": 0, "7": 3}))

    result = await server.montycat_list_memories(keyspace="vol_ks", limit=5, recent=False)

    assert result["status"] is True
    assert result["payload"] == ["k1", "k2"]
    assert calls == [{"volumes": ["0", "7"], "latest_volume": False}]


@pytest.mark.asyncio
async def test_in_memory_falls_back_to_a_volume_scan_when_latest_is_empty(server, listing):
    """The default `recent=True` path crashed too, once the newest volume was
    empty and the fallback fired."""
    inmem, _persistent, calls = _keyspace_pair([], ["k1"])
    listing(inmem, _structure({"0": 0, "7": 1}))

    result = await server.montycat_list_memories(keyspace="vol_ks", limit=5)

    assert result["status"] is True
    assert result["payload"] == ["k1"]
    assert calls == [
        {"volumes": [], "latest_volume": True},
        {"volumes": ["0", "7"], "latest_volume": False},
    ]


@pytest.mark.asyncio
async def test_in_memory_keyspace_with_no_volumes_lists_empty(server, listing):
    """A keyspace the structure does not publish must list as empty rather than
    reach the client with an empty `volumes`, which it rejects."""
    inmem, _persistent, calls = _keyspace_pair([], ["k1"])
    listing(inmem, _structure({}))

    result = await server.montycat_list_memories(keyspace="vol_ks", limit=5, recent=False)

    assert result == {"status": True, "payload": [], "error": None}
    assert calls == []


@pytest.mark.asyncio
async def test_persistent_keyspace_still_scans_by_range(server, listing):
    """The persistent client keeps its inclusive [start, stop] range, over-read
    by one and trimmed by the limit slice."""
    _inmem, persistent, calls = _keyspace_pair([], ["k1", "k2", "k3"])
    listing(persistent, _structure({}))

    result = await server.montycat_list_memories(keyspace="range_ks", limit=2, recent=False)

    assert result["status"] is True
    assert result["payload"] == ["k1", "k2"], "limit must still trim the over-read"
    assert calls == [{"limit": [0, 2], "volumes": [], "latest_volume": False}]


@pytest.mark.asyncio
@pytest.mark.parametrize("bounds, expected", [
    ({"since": "2026-09-22T00:00:00Z"},
     {"after_timestamp": "2026-09-22T00:00:00Z"}),
    ({"until": "2026-09-25T00:00:00Z"},
     {"before_timestamp": "2026-09-25T00:00:00Z"}),
    ({"since": "2026-09-22", "until": "2026-09-25"},
     {"range_timestamp": ["2026-09-22", "2026-09-25"]}),
])
async def test_time_bounded_listing_uses_native_timestamp_filter(
    server, monkeypatch, bounds, expected
):
    calls = []

    class FilteredKeyspace:
        @classmethod
        async def lookup_values_where(cls, **kwargs):
            calls.append(kwargs)
            return {"status": True, "payload": [], "error": None}

    async def bind(*_args, **_kwargs):
        return FilteredKeyspace

    monkeypatch.setattr(server, "_bind", bind)
    result = await server.montycat_list_memories(
        keyspace="POReq", limit=150, timestamp_field="createdon", **bounds
    )

    assert result["status"] is True
    assert calls[0]["limit"] == 150
    assert calls[0]["key_included"] is True
    assert calls[0]["createdon"].serialize() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs, message", [
    ({"timestamp_field": " "}, "timestamp_field"),
    ({"since": ""}, "since"),
    ({"until": " "}, "until"),
])
async def test_invalid_list_time_bounds_are_rejected_before_binding(
    server, monkeypatch, kwargs, message
):
    called = False

    async def bind(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(server, "_bind", bind)
    with pytest.raises(ValueError, match=message):
        await server.montycat_list_memories(keyspace="POReq", **kwargs)
    assert called is False
