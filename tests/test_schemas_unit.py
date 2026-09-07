"""Enforced-schema inspection tool behavior."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [True, False])
async def test_lists_schemas_from_detected_keyspace_type(
    server, monkeypatch, persistent
):
    calls = []
    response = {
        "status": True,
        "payload": {
            "Account": {"name": "String", "age": "Int", "active": "Bool"}
        },
        "error": None,
    }

    class FakeKeyspace:
        async def list_all_schemas_in_keyspace(self):
            calls.append("list")
            return response

    async def resolve(name):
        calls.append(("resolve", name))
        return persistent

    def bind(name, persistent=None):
        calls.append(("bind", name, persistent))
        return FakeKeyspace()

    monkeypatch.setattr(server, "_resolve_persistent", resolve)
    monkeypatch.setattr(server, "_keyspace", bind)

    result = await server.montycat_list_enforced_schemas(keyspace="accounts")

    assert result is response
    assert calls == [
        ("resolve", "accounts"),
        ("bind", "accounts", persistent),
        "list",
    ]


@pytest.mark.asyncio
async def test_scope_resolution_matches_other_memory_tools(server, monkeypatch):
    calls = []

    async def resolve(name):
        calls.append(name)
        return True

    class FakeKeyspace:
        async def list_all_schemas_in_keyspace(self):
            return {"status": True, "payload": [], "error": None}

    monkeypatch.setattr(server, "_resolve_persistent", resolve)
    monkeypatch.setattr(server, "_keyspace", lambda *_args, **_kwargs: FakeKeyspace())

    await server.montycat_list_enforced_schemas(scope="alice")
    await server.montycat_list_enforced_schemas(scope="shared")

    assert calls == [server._scope_prefix() + "alice", server._shared_keyspace()]


@pytest.mark.asyncio
async def test_missing_keyspace_is_read_only_and_not_auto_provisioned(
    server, monkeypatch
):
    async def missing(_name):
        return None

    monkeypatch.setattr(server, "_resolve_persistent", missing)
    monkeypatch.setattr(
        server,
        "_keyspace",
        lambda *_args, **_kwargs: pytest.fail("must not bind or create a missing keyspace"),
    )

    result = await server.montycat_list_enforced_schemas(keyspace="missing")

    assert result["status"] is False
    assert "does not exist" in result["error"]


@pytest.mark.asyncio
async def test_schema_engine_failure_is_normalized(server, monkeypatch):
    async def resolve(_name):
        return True

    class FakeKeyspace:
        async def list_all_schemas_in_keyspace(self):
            return "Error: connection reset"

    monkeypatch.setattr(server, "_resolve_persistent", resolve)
    monkeypatch.setattr(server, "_keyspace", lambda *_args, **_kwargs: FakeKeyspace())

    result = await server.montycat_list_enforced_schemas(keyspace="accounts")

    assert result["status"] is False
    assert "connection reset" in result["error"]
