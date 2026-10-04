"""Client-failure normalisation.

The Montycat client signals transport failures by *returning* the string
`"Error: ..."` rather than raising (`send_data` catches everything). Passed
through untouched, an agent receives that string where it expects a result and
has no way to tell a failure from data — the same silent-failure shape that let
a dead subscription report "nothing changed".

No engine required: these drive the boundary helper directly.
"""

from __future__ import annotations

import pytest

CONNECT_ERROR = "Error: [Errno 61] Connect call failed ('127.0.0.1', 21210)"


@pytest.fixture
def call(server):
    return server._call


async def _returns(value):
    return value


async def _raises(exc):
    raise exc


def test_error_string_is_recognised(server):
    assert server._is_client_error(CONNECT_ERROR)
    assert not server._is_client_error({"status": True})
    assert not server._is_client_error("Errors are not always fatal")


@pytest.mark.asyncio
async def test_error_string_becomes_a_failure_envelope(call):
    result = await call(_returns(CONNECT_ERROR))

    assert result["status"] is False
    assert result["payload"] is None
    assert "Connect call failed" in result["error"]
    assert "MONTYCAT_URI" in result["error"], "should say how to fix it"


@pytest.mark.asyncio
async def test_successful_result_passes_through_untouched(call):
    payload = {"status": True, "payload": "key-123", "error": None}
    assert await call(_returns(payload)) is payload


@pytest.mark.asyncio
async def test_ordinary_strings_are_not_mistaken_for_errors(call):
    """Only the client's `Error:` prefix means failure — a stored value that
    happens to be a string must survive."""
    assert await call(_returns("Error handling is hard")) == "Error handling is hard"


@pytest.mark.asyncio
async def test_unexpected_exception_becomes_a_failure_envelope(call):
    result = await call(_raises(ConnectionResetError("engine went away")))

    assert result["status"] is False
    assert "ConnectionResetError" in result["error"]
    assert "engine went away" in result["error"]


@pytest.mark.asyncio
async def test_value_error_still_propagates(call):
    """Bad caller input is the tool's own contract (empty query, missing key) —
    it must keep raising rather than being flattened into a failure payload."""
    with pytest.raises(ValueError):
        await call(_raises(ValueError("No query text provided")))


def test_uri_connection_can_opt_into_tls(server, monkeypatch):
    monkeypatch.setattr(server, "_engine", None)
    monkeypatch.setenv("MONTYCAT_URI", "montycat://user:password@localhost:21210/store")
    monkeypatch.setenv("MONTYCAT_TLS", "true")

    engine = server._get_engine()
    assert engine.tls is True
    assert engine.tls_options.verification is False
    assert engine.tls_options.pinned is False
    server._engine = None


def test_tls_verification_settings_are_strictly_optional(server, monkeypatch):
    monkeypatch.setattr(server, "_engine", None)
    monkeypatch.setenv("MONTYCAT_URI", "montycat://user:password@localhost:21210/store")
    for name in (
        "MONTYCAT_TLS",
        "MONTYCAT_TLS_VERIFY",
        "MONTYCAT_TLS_CERTIFICATE_PATH",
        "MONTYCAT_TLS_CERTIFICATE_FINGERPRINT",
        "MEMOCAT_TLS",
        "MEMOCAT_TLS_VERIFY",
        "MEMOCAT_TLS_CERTIFICATE_PATH",
        "MEMOCAT_TLS_CERTIFICATE_FINGERPRINT",
    ):
        monkeypatch.delenv(name, raising=False)

    engine = server._get_engine()
    assert engine.tls is False
    assert engine.tls_options.verification is False
    assert engine.tls_options.pinned is False
    server._engine = None


def test_false_platform_verification_keeps_legacy_tls_behavior(server, monkeypatch):
    monkeypatch.setattr(server, "_engine", None)
    monkeypatch.setenv("MONTYCAT_URI", "montycat://user:password@localhost:21210/store")
    monkeypatch.setenv("MONTYCAT_TLS", "true")
    monkeypatch.setenv("MONTYCAT_TLS_VERIFY", "false")

    engine = server._get_engine()
    assert engine.tls is True
    assert engine.tls_options.verification is False
    assert engine.tls_options.pinned is False
    server._engine = None


def test_uri_connection_can_pin_tls_certificate(server, monkeypatch):
    monkeypatch.setattr(server, "_engine", None)
    monkeypatch.setenv("MONTYCAT_URI", "montycat://user:password@localhost:21210/store")
    monkeypatch.setenv("MONTYCAT_TLS", "true")
    monkeypatch.setenv("MONTYCAT_TLS_VERIFY", "false")
    monkeypatch.setenv(
        "MONTYCAT_TLS_CERTIFICATE_FINGERPRINT",
        ":".join(["AA"] * 32),
    )

    engine = server._get_engine()
    assert engine.tls is True
    assert engine.tls_options.pinned is True
    assert engine.tls_options.verification is True
    server._engine = None


@pytest.mark.asyncio
async def test_tool_inputs_are_validated_before_engine_access(server):
    with pytest.raises(ValueError, match="positive integer"):
        await server.montycat_semantic_search(query="anything", keyspace="k", limit=0)
    with pytest.raises(ValueError, match="between -1 and 1"):
        await server.montycat_semantic_search(query="anything", keyspace="k", min_score=2)
    with pytest.raises(ValueError, match="non-empty JSON object"):
        await server.montycat_remember(value={}, keyspace="k")
    with pytest.raises(ValueError, match="non-empty list"):
        await server.montycat_remember_bulk(values=[{"ok": True}, "bad"], keyspace="k")


@pytest.mark.asyncio
async def test_keyspace_discovery_failure_is_a_tool_failure_envelope(server, monkeypatch):
    class BrokenEngine:
        async def get_structure_available(self):
            return "Error: connection reset"

    monkeypatch.setattr(server, "_engine", BrokenEngine())
    server._keyspaces.clear()
    server._ks_type_cache.clear()

    result = await server.montycat_remember(value={"text": "x"}, keyspace="private")

    assert result["status"] is False
    assert "Could not inspect keyspace" in result["error"]
    server._keyspaces.clear()
    server._ks_type_cache.clear()
    server._engine = None


@pytest.mark.asyncio
async def test_missing_keyspace_without_auto_provision_is_explicit(server, monkeypatch):
    class EmptyEngine:
        async def get_structure_available(self):
            return {"status": True, "payload": {"structure": {}}, "error": None}

    monkeypatch.setattr(server, "_engine", EmptyEngine())
    monkeypatch.setenv("MONTYCAT_AUTO_PROVISION", "false")
    server._keyspaces.clear()
    server._ks_type_cache.clear()

    result = await server.montycat_remember(value={"text": "x"}, keyspace="private")

    assert result["status"] is False
    assert "AUTO_PROVISION is disabled" in result["error"]
    server._keyspaces.clear()
    server._ks_type_cache.clear()
    server._engine = None


@pytest.mark.asyncio
async def test_failed_explicit_creation_does_not_poison_keyspace_type_cache(server, monkeypatch):
    async def engine_ready():
        return None

    class FailingKeyspace:
        async def create_keyspace(self, **_kwargs):
            return "Error: permission denied"

    server._ks_type_cache.clear()
    monkeypatch.setattr(server, "_engine_ready", engine_ready)
    monkeypatch.setattr(server, "_keyspace", lambda *_args, **_kwargs: FailingKeyspace())

    result = await server.montycat_create_keyspace(keyspace="private", persistent=False)

    assert result["status"] is False
    assert "private" not in server._ks_type_cache


@pytest.mark.asyncio
async def test_create_keyspace_prefers_storage_and_keeps_persistent_compatibility(
    server, monkeypatch
):
    calls = []

    async def engine_ready():
        return None

    class FakeKeyspace:
        async def create_keyspace(self, **kwargs):
            calls.append(kwargs)
            return {"status": True, "payload": "created", "error": None}

    def fake_keyspace(name, persistent=None):
        calls.append((name, persistent))
        return FakeKeyspace()

    server._ks_type_cache.clear()
    monkeypatch.setattr(server, "_engine_ready", engine_ready)
    monkeypatch.setattr(server, "_keyspace", fake_keyspace)

    result = await server.montycat_create_keyspace(
        keyspace="working", storage="inmemory"
    )
    legacy = await server.montycat_create_keyspace(
        keyspace="durable", persistent=True, cache=20, compression=True
    )

    assert result["status"] is True
    assert legacy["status"] is True
    assert calls == [
        ("working", False),
        {"semantic": False},
        ("durable", True),
        {
            "cache": 20,
            "compression": True,
            "semantic": False,
        },
    ]
    assert server._ks_type_cache["working"] is False
    assert server._ks_type_cache["durable"] is True


@pytest.mark.asyncio
async def test_create_keyspace_rejects_invalid_or_conflicting_schema_before_engine(
    server,
):
    with pytest.raises(ValueError, match="non-empty"):
        await server.montycat_create_keyspace(keyspace=" ")
    with pytest.raises(ValueError, match="persistent.*inmemory"):
        await server.montycat_create_keyspace(keyspace="k", storage="disk")
    with pytest.raises(ValueError, match="conflicting"):
        await server.montycat_create_keyspace(
            keyspace="k", storage="inmemory", persistent=True
        )
    with pytest.raises(ValueError, match="only for persistent"):
        await server.montycat_create_keyspace(
            keyspace="k", storage="inmemory", cache=10
        )
    with pytest.raises(ValueError, match="semantic_model"):
        await server.montycat_create_keyspace(
            keyspace="k", semantic_model="not-a-model"
        )


@pytest.mark.asyncio
async def test_update_cache_uses_persistent_client_surface(server, monkeypatch):
    calls = []

    class Keyspace:
        async def update_cache(self, **kwargs):
            calls.append(kwargs)
            return {"status": True, "payload": "updated", "error": None}

    async def bind(name):
        assert name == "durable"
        return Keyspace()

    async def resolve(name):
        assert name == "durable"
        return True

    monkeypatch.setattr(server, "_bind", bind)
    monkeypatch.setattr(server, "_resolve_persistent", resolve)

    result = await server.montycat_update_cache(keyspace="durable", cache=32)

    assert result["status"] is True
    assert calls == [{"cache": 32}]


@pytest.mark.asyncio
async def test_update_cache_rejects_invalid_cache_and_inmemory(server, monkeypatch):
    with pytest.raises(ValueError, match="positive integer"):
        await server.montycat_update_cache(keyspace="k", cache=0)

    async def bind(_name):
        return object()

    async def resolve(_name):
        return False

    monkeypatch.setattr(server, "_bind", bind)
    monkeypatch.setattr(server, "_resolve_persistent", resolve)
    with pytest.raises(ValueError, match="persistent keyspaces"):
        await server.montycat_update_cache(keyspace="working", cache=10)


@pytest.mark.asyncio
async def test_create_keyspace_can_enable_scoped_semantic_search(server, monkeypatch):
    class FakeKeyspace:
        def __init__(self):
            self.creation_call = None

        async def create_keyspace(self, **kwargs):
            self.creation_call = kwargs
            return {"status": True, "payload": "created", "error": None}

    class FakeEngine:
        store = "memories"

        def __init__(self):
            self.semantic_call = None

        async def enable_semantic_search(self, **kwargs):
            self.semantic_call = kwargs
            return {
                "status": True,
                "payload": {"model": kwargs["model"].value},
                "error": None,
            }

    engine = FakeEngine()
    keyspace = FakeKeyspace()
    server._ks_type_cache.clear()
    monkeypatch.setattr(server, "_engine", engine)
    monkeypatch.setattr(server, "_keyspace", lambda *_args, **_kwargs: keyspace)

    result = await server.montycat_create_keyspace(
        keyspace="research",
        storage="persistent",
        semantic_model="bge-small",
    )

    assert result["status"] is True
    assert result["payload"]["semantic"] is True
    assert result["payload"]["semantic_model"] == "bge-small"
    assert keyspace.creation_call == {
        "cache": None,
        "compression": False,
        "semantic": True,
    }
    assert engine.semantic_call["store"] == "memories"
    assert engine.semantic_call["keyspace"] == "research"
    assert engine.semantic_call["model"].value == "bge-small"


@pytest.mark.asyncio
async def test_every_tool_reports_failure_when_the_engine_is_unreachable(server, monkeypatch):
    """End-to-end shape check: with no engine, each tool must return a failure
    envelope — never a bare `"Error: ..."` string, which an agent would read as
    a result. Uses a closed port, so it needs no database.
    """
    monkeypatch.setattr(server, "_engine", None)
    monkeypatch.setenv("MONTYCAT_URI", "montycat://x:y@127.0.0.1:59997/deadstore")
    server._keyspaces.clear()
    server._ks_type_cache.clear()

    tools = {
        "semantic_search": server.montycat_semantic_search(query="anything", keyspace="k"),
        "remember": server.montycat_remember(value={"a": 1}, keyspace="k"),
        "remember_bulk": server.montycat_remember_bulk(values=[{"a": 1}], keyspace="k"),
        "recall": server.montycat_recall(keyspace="k", key="1"),
        "list_memories": server.montycat_list_memories(keyspace="k"),
        "list_keyspaces": server.montycat_list_keyspaces(),
        "policy_view": server.montycat_policy_view(),
        "policy_history": server.montycat_policy_history(),
        "policy_explain": server.montycat_policy_explain(
            capability="manage-semantic"
        ),
        "create_keyspace": server.montycat_create_keyspace(keyspace="k"),
        "update_cache": server.montycat_update_cache(keyspace="k", cache=10),
        "remove_keyspace": server.montycat_remove_keyspace(keyspace="k"),
        "enable_semantic": server.montycat_enable_semantic(keyspace="k"),
        "disable_semantic": server.montycat_disable_semantic(keyspace="k"),
        "start_snapshots": server.montycat_start_snapshots(keyspace="k"),
        "stop_snapshots": server.montycat_stop_snapshots(keyspace="k"),
        "clean_snapshots": server.montycat_clean_snapshots(keyspace="k"),
        "update": server.montycat_update(updates={"a": 2}, keyspace="k", key="1"),
        "forget": server.montycat_forget(keyspace="k", key="1"),
        "await_change": server.montycat_await_memory_change(keyspace="k", timeout_sec=2),
    }

    for name, coro in tools.items():
        result = await coro
        assert isinstance(result, dict), f"{name} returned a bare {type(result).__name__}"
        assert result.get("status") is False, f"{name} did not report failure"
        assert result.get("error"), f"{name} reported no reason"

    # caches were populated against the dead engine; leave them clean for others
    server._keyspaces.clear()
    server._ks_type_cache.clear()
