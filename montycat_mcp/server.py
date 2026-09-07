"""Montycat MCP server.

Exposes a Montycat engine as shared, persistent memory for MCP-compatible AI
systems. Agents connected to the same engine and keyspace can store facts,
recall them by meaning or key, and react to each other's updates while the data
remains on user-controlled hardware.

Connection is configured from the environment:

    MONTYCAT_URI           montycat://user:pass@host:port/store   (preferred)

  or the discrete parts:

    MONTYCAT_HOST          default 127.0.0.1
    MONTYCAT_PORT          default 21210
    MONTYCAT_USERNAME
    MONTYCAT_PASSWORD
    MONTYCAT_STORE
    MONTYCAT_TLS           "true"/"false", default false

  behavior:

    MONTYCAT_DEFAULT_KEYSPACE   default "memory" — used when a tool omits scope/keyspace
    MONTYCAT_PERSISTENT         "true"/"false", default true — new keyspaces persist
    MONTYCAT_SCOPE              default owner/scope, used when a tool omits `scope`
    MONTYCAT_SCOPE_PREFIX       default "mem_" — per-owner keyspace prefix
    MONTYCAT_SHARED_KEYSPACE    default "mem_shared" — the common/shared keyspace
    MONTYCAT_AUTO_PROVISION     "true"/"false", default true — create a scope's
                                keyspace on first use (requires provisioning
                                authority for the configured owner)
    MONTYCAT_AUTO_TIMESTAMP     "true"/"false", default true — stamp each memory
                                with an indexed `_created_at`, enabling
                                time-range recall (`since`/`until`). Costs a
                                server-side timestamp parse per write; turn off
                                if memories are never recalled by time.

  real-time watch (§7.3):

    MONTYCAT_SUBSCRIPTION_PORT  default: main port + 1 (21211) — the engine's
                                subscription server, enabled by default
    MONTYCAT_WATCH_BUFFER       default 500 — changes retained per watched
                                keyspace, so changes between calls aren't lost
    MONTYCAT_WATCH_IDLE_TIMEOUT default 300 (seconds) — close a subscription
                                nobody is waiting on
    MONTYCAT_WATCH_AUTH_LEASE_SEC default 5 — revalidate read authority for
                                active watches; access loss purges the buffer

Scoping: pass `scope` (an owner/user id) to any memory tool and it targets that
owner's private keyspace `mem_<scope>` — isolated semantic recall per owner. The
special scope "shared" targets the common keyspace all owners can use. Omit scope
to use MONTYCAT_SCOPE, then the default keyspace. Isolation is by keyspace, which
also aligns with Montycat's per-keyspace RBAC (grant owners access to their own).

Semantic search requires the Montycat **Semantic** edition (it is enabled there
by default).

Real-time watch: Montycat pushes every change over a live subscription, so an
agent can be *told* its memory changed instead of polling — see `watch.py` and
`montycat_await_memory_change`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import suppress
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from montycat import (
    Engine,
    Keyspace,
    PolicyCapability,
    PolicyKeyspaceType,
    ResultOrder,
    SearchMode,
    SemanticModel,
    Timestamp,
)

from .watch import registry as watch_registry

SERVER_INSTRUCTIONS = """Montycat MCP is shared, persistent memory for AI agents and
systems. Search it for relevant prior context before asking the user to repeat
information. Remember durable preferences, facts, and decisions when the user
asks or when they will clearly be useful in a later conversation; update stale
facts instead of creating conflicting duplicates. Search by meaning by default;
switch to mode='keyword' when the query hinges on an exact term such as an
identifier or error code, and mode='hybrid' when it is both. Use scope='shared'
only when the user intends other authorized agents to access the memory. Sharing requires
clients to connect to the same Montycat engine and keyspace—separate local
engines do not synchronize. When a structured write or retrieval must use an
existing schema and its field names or types are uncertain, inspect that keyspace
with montycat_list_enforced_schemas first; do not perform a schema lookup for
ordinary schema-free memory. Do not store secrets or full conversation transcripts
by default."""

mcp = FastMCP("montycat", instructions=SERVER_INSTRUCTIONS)

# Safety metadata returned by MCP tools/list. Mutating tools are identified
# truthfully; each MCP host decides which operations require user confirmation.
READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
MUTATING = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)
DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
)
# Installing the engine reaches the network and opens the OS installer behind an
# administrator prompt, so it requires explicit confirmation.
INSTALLS_SOFTWARE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True
)

_engine: Optional[Engine] = None
_keyspaces: dict[tuple[str, bool], Any] = {}
# name -> is-persistent, learned from the engine's structure (or recorded on create)
_ks_type_cache: dict[str, bool] = {}


class KeyspaceBindingError(RuntimeError):
    """The server could not safely determine or provision a keyspace."""


class EngineStarting(RuntimeError):
    """Bootstrap is still running; the engine is not reachable yet."""


def _now_iso() -> str:
    """UTC wall clock as the ISO-8601 shape the engine's timestamp index
    parses (`2026-07-20T14:30:00`). Every remember stamps `_created_at` with
    this, which is what makes time-range recall (`since`/`until`) work."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _is_indexable_timestamp(text: Any) -> bool:
    """Whether the engine's timestamp parser will accept this string. Guards
    the hoist below: a `timestamps` entry the engine cannot parse fails the
    whole write, so anything unrecognized is left as an ordinary field."""
    if not isinstance(text, str):
        return False
    try:
        datetime.fromisoformat(text)
        return True
    except ValueError:
        return False


def _stamp(value: dict, enabled: Optional[bool] = None) -> dict:
    """Give a memory an indexed `_created_at`, unless timestamping is off.

    The engine only puts a field in its *timestamp* index if it arrives nested
    under a `timestamps` object; it parses those, then flattens them back to
    the top level of the stored value. A plain top-level date string is just a
    string in the kv index — matchable by exact equality, never by range — so
    `since`/`until` recall would silently return nothing.

    Timestamping costs a server-side regex/parse sweep per write, so it can be
    turned off (`MONTYCAT_AUTO_TIMESTAMP=false`, or `timestamp=False` on a
    single call) when memories are never recalled by time. With it off,
    `since`/`until` have nothing to match — the trade is explicit.

    A caller-supplied `_created_at` (historical import) is hoisted into
    `timestamps` so it is range-queryable too, but only when it parses: an
    unparseable entry there would fail the whole insert.
    """
    if enabled is None:
        enabled = _env_bool("MONTYCAT_AUTO_TIMESTAMP", True)
    if not enabled:
        return value

    value = {**value}
    stamps = dict(value.get("timestamps") or {})

    supplied = value.get("_created_at")
    if supplied is not None and "_created_at" not in stamps:
        if not _is_indexable_timestamp(supplied):
            return value  # unparseable — leave the caller's value untouched
        stamps["_created_at"] = value.pop("_created_at")

    stamps.setdefault("_created_at", _now_iso())
    value["timestamps"] = stamps
    return value


def _failure(message: str) -> dict:
    """The response envelope the engine itself uses, so a client-side problem
    reaches the agent in the same shape as a server-side one."""
    return {"status": False, "payload": None, "error": message}


def _is_client_error(result: Any) -> bool:
    """The Montycat client reports transport failures by *returning* the string
    `"Error: ..."` rather than raising (see `send_data`). Unrecognised, that
    string reaches the agent as if it were a result."""
    return isinstance(result, str) and result.startswith("Error:")


async def _call(awaitable: Any) -> Any:
    """Await a client call and normalise its failure modes.

    Two things can go wrong and neither arrives as a usable response:
      * the client returns `"Error: ..."` (connection refused, timeout, TLS);
      * the call raises (bad arguments, serialisation).

    Both become a `{status: False, error: ...}` envelope, so an agent is told
    the operation failed instead of receiving a string that looks like data.
    """
    try:
        result = await awaitable
    except ValueError:
        raise  # caller-input errors are the tool's own contract — let them out
    except Exception as exc:  # noqa: BLE001 - reported to the agent, not handled
        return _failure(f"{type(exc).__name__}: {exc}")

    if _is_client_error(result):
        detail = result[len("Error:"):].strip()
        return _failure(
            f"Montycat engine call failed: {detail}. Check the engine is running "
            f"and reachable at the configured MONTYCAT_URI / host and port."
        )
    return result


def _engine_port() -> int:
    """The engine's main port, for error messages. The subscription server
    listens on this + 1 unless MONTYCAT_SUBSCRIPTION_PORT overrides it."""
    engine = _get_engine()
    port = getattr(engine, "port", None)
    if isinstance(port, int):
        return port
    return int(os.environ.get("MONTYCAT_PORT", "21210"))


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None and name.startswith("MONTYCAT_"):
        val = os.environ.get(f"MEMOCAT_{name.removeprefix('MONTYCAT_')}")
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _get_engine() -> Engine:
    """Build the shared Engine once, from the environment."""
    global _engine
    if _engine is not None:
        return _engine

    uri = os.environ.get("MONTYCAT_URI")
    if uri:
        _engine = Engine.from_uri(uri)
        # URI syntax identifies the endpoint and credentials; TLS remains an
        # explicit opt-in so existing montycat:// configurations keep their
        # plaintext behavior.
        _engine.tls = _env_bool("MONTYCAT_TLS", False)
    else:
        _engine = Engine(
            host=os.environ.get("MONTYCAT_HOST", "127.0.0.1"),
            port=int(os.environ.get("MONTYCAT_PORT", "21210")),
            username=os.environ.get("MONTYCAT_USERNAME", ""),
            password=os.environ.get("MONTYCAT_PASSWORD", ""),
            store=os.environ.get("MONTYCAT_STORE"),
            tls=_env_bool("MONTYCAT_TLS", False),
        )
    return _engine


def _validate_limit(limit: int, *, name: str = "limit") -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError(f"{name} must be a positive integer.")


# Each ranking mode scores on its own scale, so one bound cannot police them
# all: cosine similarity is [-1, 1], engine-normalized hybrid RRF is [0, 1], and
# raw BM25 is unbounded above — a keyword floor of 8.0 is legitimate.
_SCORE_RANGE: dict[str, tuple[float, Optional[float]]] = {
    "semantic": (-1.0, 1.0),
    "hybrid": (0.0, 1.0),
    "keyword": (0.0, None),
}


def _validate_mode(mode: str) -> SearchMode:
    try:
        return SearchMode(mode)
    except ValueError:
        raise ValueError(
            "mode must be one of: semantic, keyword, hybrid."
        ) from None


def _validate_score(min_score: Optional[float], mode: str = "semantic") -> None:
    if min_score is None:
        return
    if isinstance(min_score, bool) or not isinstance(min_score, (int, float)):
        raise ValueError("min_score must be a number.")
    low, high = _SCORE_RANGE[mode]
    if min_score < low or (high is not None and min_score > high):
        bound = f"between {low:g} and {high:g}" if high is not None else f"at least {low:g}"
        raise ValueError(f"min_score must be {bound} in {mode} mode.")


def _default_keyspace() -> str:
    # A cleared MCPB settings field arrives as an empty string, not an absent
    # variable, so `get(..., default)` alone would hand every tool "" as the
    # keyspace. Treat blank as unset.
    return os.environ.get("MONTYCAT_DEFAULT_KEYSPACE", "").strip() or "memory"


def _scope_prefix() -> str:
    return os.environ.get("MONTYCAT_SCOPE_PREFIX", "mem_")


def _shared_keyspace() -> str:
    return os.environ.get("MONTYCAT_SHARED_KEYSPACE", "mem_shared")


def _resolve_keyspace(scope: Optional[str] = None, keyspace: Optional[str] = None) -> str:
    """Turn a scope/keyspace into the concrete keyspace name to target.

    Precedence:
      1. explicit `keyspace`      -> used verbatim (escape hatch)
      2. `scope` (or MONTYCAT_SCOPE env) -> per-owner keyspace `mem_<scope>`;
         the special scope `"shared"` maps to the common keyspace
      3. neither                  -> the default keyspace
    """
    if keyspace:
        return keyspace
    scope = scope or os.environ.get("MONTYCAT_SCOPE")
    if scope:
        if scope == "shared" or scope == _shared_keyspace():
            return _shared_keyspace()
        return f"{_scope_prefix()}{scope}"
    return _default_keyspace()


def _keyspace(name: Optional[str], persistent: Optional[bool] = None):
    """Bind a Montycat keyspace class by name (built and cached on first use).

    The Montycat client models keyspaces as classes; MCP tools pass a string, so
    we synthesize the subclass on the fly and connect it to the shared engine.
    """
    name = name or _default_keyspace()
    if persistent is None:
        persistent = _env_bool("MONTYCAT_PERSISTENT", True)

    cache_key = (name, persistent)
    cached = _keyspaces.get(cache_key)
    if cached is not None:
        return cached

    base = Keyspace.Persistent if persistent else Keyspace.InMemory
    cls = type(name, (base,), {"keyspace": name})
    cls.connect_engine(_get_engine())
    _keyspaces[cache_key] = cls
    return cls


async def _resolve_persistent(name: str) -> Optional[bool]:
    """Detect whether an existing keyspace is persistent (True) or in-memory
    (False) from the engine's structure. Returns None if it does not exist yet.
    Result is cached per name."""
    await _engine_ready()
    if name in _ks_type_cache:
        return _ks_type_cache[name]
    res = await _call(_get_engine().get_structure_available())
    if isinstance(res, dict) and res.get("status") is False:
        raise KeyspaceBindingError(
            f"Could not inspect keyspace {name!r}: {res.get('error') or 'unknown engine error'}"
        )
    payload = res.get("payload") if isinstance(res, dict) else None
    structure = (payload or {}).get("structure") or {}
    for store in structure.values():
        if not isinstance(store, dict):
            continue
        if name in (store.get("persistent") or {}):
            _ks_type_cache[name] = True
            return True
        if name in (store.get("inmemory") or {}):
            _ks_type_cache[name] = False
            return False
    return None


async def _inmemory_volumes(name: str) -> list:
    """List the volume ids of an existing in-memory keyspace.

    The in-memory client cannot scan by range: its get_keys accepts only
    `volumes` or `latest_volume`, with no `limit` parameter. A full scan
    therefore has to name every volume, and the engine's structure is the only
    place they are published.
    """
    await _engine_ready()
    res = await _call(_get_engine().get_structure_available())
    if isinstance(res, dict) and res.get("status") is False:
        raise KeyspaceBindingError(
            f"Could not inspect keyspace {name!r}: {res.get('error') or 'unknown engine error'}"
        )
    payload = res.get("payload") if isinstance(res, dict) else None
    structure = (payload or {}).get("structure") or {}
    for store in structure.values():
        if not isinstance(store, dict):
            continue
        entry = (store.get("inmemory") or {}).get(name)
        if isinstance(entry, dict):
            return [str(volume) for volume in (entry.get("volumes") or {})]
    return []


async def _ensure_keyspace(name: str, persistent: bool) -> bool:
    """Create a missing keyspace or return the type created by another caller.

    Provisioning failures must be visible: silently selecting the configured
    storage type after an authorization or transport failure can send a request
    to the wrong keyspace tier and disguise the real cause.
    """
    ks = _keyspace(name, persistent=persistent)
    result = await _call(ks.create_keyspace())
    if not (isinstance(result, dict) and result.get("status") is False):
        _ks_type_cache[name] = persistent
        return persistent

    # A concurrent request may have created it between discovery and create.
    # Re-read the structure before treating the original failure as fatal.
    try:
        detected = await _resolve_persistent(name)
    except KeyspaceBindingError:
        detected = None
    if detected is not None:
        return detected
    original_error = result.get("error") or "unknown engine error"
    explanation = await _explain_provision_failure(name, persistent)
    detail = f" Policy explanation: {json.dumps(explanation, default=str)}" \
        if explanation is not None else ""
    raise KeyspaceBindingError(
        f"Could not auto-provision keyspace {name!r}: {original_error}.{detail}"
    )


async def _explain_provision_failure(name: str, persistent: bool) -> Optional[Any]:
    """Best-effort policy context for a failed automatic keyspace creation.

    The create attempt always happens first and its error remains primary.
    Explanation is read-only and advisory; failure to obtain it must never
    replace or hide the authoritative provisioning result.
    """
    try:
        engine = _get_engine()
        if not engine.store:
            return None
        result = await _call(engine.policy_explain(
            capability=PolicyCapability.PROVISION_KEYSPACE,
            store=engine.store,
            keyspace=name,
            keyspace_type=(
                PolicyKeyspaceType.PERSISTENT
                if persistent
                else PolicyKeyspaceType.IN_MEMORY
            ),
            model=None,
        ))
        if isinstance(result, dict) and result.get("status") is not False:
            return result.get("payload", result)
    except Exception:
        # Compatibility with older clients/engines: the original create error
        # remains sufficient and must not be masked by explanation failure.
        pass
    return None


async def _bind(name: Optional[str] = None, persistent: Optional[bool] = None):
    """Resolve and bind a keyspace, auto-detecting its persistent/in-memory type
    and auto-provisioning it on first use (per-owner scopes).

    Precedence for type: explicit `persistent` arg > the keyspace's actual type
    on the engine (self-correcting) > the `MONTYCAT_PERSISTENT` env default.
    A keyspace that does not exist yet is created when MONTYCAT_AUTO_PROVISION
    is enabled (default true).
    """
    await _engine_ready()
    name = name or _default_keyspace()
    if persistent is None:
        detected = await _resolve_persistent(name)
        if detected is None:
            persistent = _env_bool("MONTYCAT_PERSISTENT", True)
            if _env_bool("MONTYCAT_AUTO_PROVISION", True):
                persistent = await _ensure_keyspace(name, persistent)
            else:
                raise KeyspaceBindingError(
                    f"Keyspace {name!r} does not exist and MONTYCAT_AUTO_PROVISION is disabled."
                )
        else:
            persistent = detected
    return _keyspace(name, persistent=persistent)


# ── engine readiness ─────────────────────────────────────────────────────────

# Bootstrap runs as a background task so the MCP handshake is never blocked
# behind it: acquiring an engine can involve a container pull or an embedding
# model download, and no client waits minutes for `initialize`.
_bootstrap_task: Optional[asyncio.Task] = None
_bootstrap_failure: Optional[str] = None
_bootstrap_failed_at: float = 0.0
_acquisition_lock: Optional[asyncio.Lock] = None

# How long a tool waits for a pending bootstrap before reporting back instead of
# hanging. Short enough to stay inside any client's tool timeout, long enough
# that a reachable engine, an installed binary, or a warm container all resolve
# invisibly.
_READY_BUDGET = 20.0
# A bootstrap that failed is retried after this, so starting Docker mid-session
# does not require restarting the whole server.
_RETRY_COOLDOWN = 30.0


def _ready_budget() -> float:
    try:
        value = os.environ.get(
            "MONTYCAT_READY_TIMEOUT",
            os.environ.get("MEMOCAT_READY_TIMEOUT", _READY_BUDGET),
        )
        return max(0.5, float(value))
    except ValueError:
        return _READY_BUDGET


async def _bootstrap() -> str:
    from .bootstrap import ensure_engine

    global _bootstrap_failure, _bootstrap_failed_at
    try:
        async with _acquisition_guard():
            tier = await ensure_engine()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        detail = str(exc).strip()
        _bootstrap_failure = detail or f"{type(exc).__name__} while acquiring the engine."
        _bootstrap_failed_at = time.monotonic()
        raise
    _bootstrap_failure = None
    if tier != "existing":
        logging.getLogger("montycat").info("started Montycat engine via %s", tier)
    return tier


def _acquisition_guard() -> asyncio.Lock:
    """Serialize automatic acquisition and the explicit installer tool."""
    global _acquisition_lock
    if _acquisition_lock is None:
        _acquisition_lock = asyncio.Lock()
    return _acquisition_lock


def start_bootstrap() -> asyncio.Task:
    """Begin (or restart) engine acquisition in the background."""
    global _bootstrap_task
    if _bootstrap_task is None or _bootstrap_task.done():
        if _bootstrap_task is not None and not _bootstrap_task.cancelled():
            # Retrieve a background failure before replacing its task. Without
            # this, a startup failure that no tool observed emits an asyncio
            # "Task exception was never retrieved" warning at shutdown.
            with suppress(Exception):
                _bootstrap_task.exception()
        _bootstrap_task = asyncio.create_task(_bootstrap())
    return _bootstrap_task


async def _engine_ready() -> None:
    """Block until an engine is usable, or explain why it is not.

    Engine-access boundaries go through here after validating tool arguments.
    Bootstrap hands credentials over by setting environment variables, and
    `_get_engine()` reads them once and caches the Engine for the life of the
    process — so binding before bootstrap would pin empty credentials.
    """
    global _bootstrap_task
    # A cached engine proves acquisition already completed. This is also the
    # supported seam for unit tests that inject an isolated fake engine.
    if _engine is not None:
        return
    task = _bootstrap_task
    if task is None:
        task = start_bootstrap()
    elif task.done() and _bootstrap_failure is not None:
        if not task.cancelled():
            with suppress(Exception):
                task.exception()
        if time.monotonic() - _bootstrap_failed_at < _RETRY_COOLDOWN:
            raise KeyspaceBindingError(_bootstrap_failure)
        task = start_bootstrap()

    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=_ready_budget())
    except asyncio.TimeoutError:
        raise EngineStarting(
            "The Montycat engine is still starting — it may be downloading a "
            "container image or an embedding model. Ask me again in a moment."
        ) from None
    except Exception as exc:  # BootstrapError, surfaced with its instructions
        raise KeyspaceBindingError(_bootstrap_failure or str(exc)) from None


# ── tools ────────────────────────────────────────────────────────────────────


def _engine_tool(tool):
    """Normalise readiness and keyspace-binding failures from a tool.

    Readiness is checked at the engine-access boundary inside the tool, after
    its argument validation. Both failure types become ordinary MCP result
    envelopes instead of transport errors or hangs.
    """
    @wraps(tool)
    async def wrapped(*args, **kwargs):
        try:
            return await tool(*args, **kwargs)
        except (KeyspaceBindingError, EngineStarting) as exc:
            return _failure(str(exc))
    return wrapped


# Retained: `_binding_failure` was the pre-readiness name for this decorator.
_binding_failure = _engine_tool


@mcp.tool(title="Search Memories", annotations=READ_ONLY)
@_binding_failure
async def montycat_semantic_search(
    query: str = "",
    keyspace: Optional[str] = None,
    scope: Optional[str] = None,
    limit: int = 5,
    mode: str = "semantic",
    min_score: Optional[float] = None,
    filters: Optional[dict] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    vector: Optional[list[float]] = None,
) -> Any:
    """Search stored memory by MEANING, by KEYWORD, or both.

    Use this to recall relevant facts, documents, or past context for RAG and
    agent memory. Returns the top matches ranked by relevance, each with its
    key, a score, and the stored value.

    Ranking modes (`mode`):
      - "semantic" (default) — vector similarity. Finds a memory whose wording
        differs from the query. Scores are cosine similarity in [-1, 1].
      - "keyword" — BM25 over the stored text. Use it when the query contains an
        exact term that must appear: an identifier, error code, or file name.
        BM25 scores are unbounded and comparable only within one query.
      - "hybrid" — runs both and fuses them with reciprocal rank fusion. The
        safest default when a query mixes meaning with an exact term. Scores are
        normalized to [0, 1].
    Keyword and hybrid need a Montycat Semantic engine >= 1.3.4; older engines
    reject the request rather than silently returning semantic-only results.

    Narrowing is separate from ranking: `filters`, `since`, and `until` restrict
    WHICH memories are ranked — a hard AND over indexed fields — and never
    change the order within that set. Combine them freely: "what did we decide
    about the index" + `since` yesterday + `filters={"project": "montycat"}` is
    one call. A filter matching nothing returns [].

    Args:
        query: Natural-language description of what to recall. May be empty
               when `vector` supplies a precomputed query embedding.
        mode: Ranking strategy — "semantic", "keyword", or "hybrid".
        vector: Optional precomputed query embedding, for the vector half of
                "semantic" and "hybrid". It must match the keyspace's enrolled
                embedding space and dimensions; when set, the engine does not
                embed `query`.
        scope: Owner/user id to scope recall to (searches only that owner's memory,
               keyspace mem_<scope>). Use "shared" for the common keyspace.
        keyspace: Explicit keyspace override (advanced; bypasses scope).
        limit: Max number of results (default 5).
        min_score: Optional relevance floor; drops weak matches. The valid range
                   follows the mode: [-1, 1] semantic, [0, 1] hybrid, >= 0
                   keyword.
        filters: Optional metadata constraints, e.g. {"project": "x"} — only
                 memories whose indexed fields equal these values are ranked.
        since: Only memories created at/after this time (ISO-8601, UTC —
               matches the auto-stamped `_created_at`).
        until: Only memories created before this time (ISO-8601, UTC).
    """
    _validate_limit(limit)
    search_mode = _validate_mode(mode)
    _validate_score(min_score, search_mode.value)
    if not query.strip() and vector is None:
        raise ValueError("Provide a non-empty query or a precomputed vector.")
    # BM25 ranks the query text itself, so a bare vector has nothing to score
    # against; failing here beats an engine-side rejection with less context.
    if search_mode is SearchMode.KEYWORD and not query.strip():
        raise ValueError("keyword mode needs query text; a vector alone cannot be scored.")
    ks = await _bind(_resolve_keyspace(scope, keyspace))
    if since or until:
        filters = dict(filters or {})
        if since and until:
            filters["_created_at"] = Timestamp(start=since, end=until)
        elif since:
            filters["_created_at"] = Timestamp(after=since)
        else:
            filters["_created_at"] = Timestamp(before=until)
    return await _call(ks.search_values(
        query=query,
        mode=search_mode,
        filters=filters or None,
        vector=vector,
        limit=limit,
        min_score=min_score,
    ))


@mcp.tool(title="Store Memory", annotations=MUTATING)
@_binding_failure
async def montycat_remember(
    value: dict,
    keyspace: Optional[str] = None,
    scope: Optional[str] = None,
    custom_key: Optional[str] = None,
    timestamp: Optional[bool] = None,
    wait_for_index: Optional[bool] = None,
    vector: Optional[list[float]] = None,
) -> Any:
    """Store a fact or record in memory; it is embedded and indexed automatically.

    Later recall it by meaning with montycat_semantic_search, or by key with
    montycat_recall. Returns the generated key in `payload`.

    Every record is auto-stamped with an indexed `_created_at` (UTC ISO-8601)
    unless the value already carries one — this powers time-range recall
    (`since`/`until` on montycat_semantic_search). Top-level fields are
    indexed, so they can be used as `filters` in hybrid search (e.g. store
    `{"project": "x", ...}`, later filter on it).

    Args:
        value: The record to store (a JSON object).
        scope: Owner/user id to store under (that owner's private memory,
               keyspace mem_<scope>). Use "shared" for the common keyspace.
        keyspace: Explicit keyspace override (advanced; bypasses scope).
        custom_key: Optional stable key to store under (for later exact recall/update).
        timestamp: Index a `_created_at` for time-range recall. Defaults to
                   MONTYCAT_AUTO_TIMESTAMP (on). Pass False to skip the
                   server-side timestamp parse when this memory will never be
                   recalled by time.
        wait_for_index: For persistent keyspaces, wait until secondary indexes
                        have caught up before returning. Defaults to the engine
                        setting; use True when an immediate filtered/semantic
                        recall must see this write.
        vector: Optional precomputed embedding for this record. It must match
                the keyspace's enrolled embedding profile.
    """
    if not isinstance(value, dict) or not value:
        raise ValueError("value must be a non-empty JSON object.")
    ks = await _bind(_resolve_keyspace(scope, keyspace))
    value = _stamp(value, timestamp)
    if custom_key is not None:
        return await _call(ks.insert_custom_key_value(
            custom_key, value, vector=vector, wait_for_index=wait_for_index
        ))
    return await _call(ks.insert_value(
        value, vector=vector, wait_for_index=wait_for_index
    ))


@mcp.tool(title="Recall Memories", annotations=READ_ONLY)
@_binding_failure
async def montycat_recall(
    keyspace: Optional[str] = None,
    scope: Optional[str] = None,
    key: Optional[str] = None,
    custom_key: Optional[str] = None,
    filters: Optional[dict] = None,
    limit: int = 25,
) -> Any:
    """Recall memory by exact key or by field filter (not by meaning).

    Provide `key`/`custom_key` to fetch a single record, or `filters` (a map of
    field -> value) to look up all records matching those fields. For meaning-based
    recall use montycat_semantic_search instead.

    Args:
        keyspace: Memory namespace (defaults to the configured one).
        key: Montycat-generated key to fetch.
        custom_key: Custom key to fetch.
        filters: Field equality filters, e.g. {"user": "alice", "topic": "billing"}.
        limit: Max results for a filter lookup (default 25).
    """
    _validate_limit(limit)
    if key is None and custom_key is None and not filters:
        raise ValueError("Provide one of: key, custom_key, or filters.")
    ks = await _bind(_resolve_keyspace(scope, keyspace))
    if key is not None or custom_key is not None:
        return await _call(ks.get_value(key=key, custom_key=custom_key))
    if filters:
        return await _call(ks.lookup_values_where(limit=limit, key_included=True, **filters))
    raise AssertionError("validated recall selector was not handled")


@mcp.tool(title="Install Montycat Engine", annotations=INSTALLS_SOFTWARE)
async def montycat_install_engine() -> Any:
    """Install the Montycat engine on THIS computer, then start it.

    Call this only when memory tools report that no engine is running and the
    user has agreed to install one. Tell them what it does first: it downloads
    the Montycat Semantic package (~18 MB) and opens your operating system's
    installer, which asks for an administrator password. On Linux it runs the
    documented APT installation with `sudo`.

    Refuses when MONTYCAT_URI is set or the configured host is not this
    machine — Montycat MCP is pointed at an engine elsewhere, and installing a local
    one would create a second database and write memories where nobody is
    looking. Does nothing if an engine is already reachable.

    Not needed when Docker is available: engine startup falls back to a
    container automatically, with no prompt.
    """
    from .bootstrap import BootstrapError, install_engine

    global _bootstrap_failure
    try:
        async with _acquisition_guard():
            message = await install_engine()
    except BootstrapError as exc:
        return _failure(str(exc))
    # The engine is up and its credentials are published; let the next tool call
    # proceed instead of replaying a cached failure through the cooldown.
    _bootstrap_failure = None
    start_bootstrap()
    return {"status": True, "payload": {"detail": message}, "error": None}


@mcp.tool(title="List Memory Keyspaces", annotations=READ_ONLY)
@_engine_tool
async def montycat_list_keyspaces() -> Any:
    """List the available memory stores and keyspaces on this Montycat engine."""
    await _engine_ready()
    return await _call(_get_engine().get_structure_available())


@mcp.tool(title="List Enforced Schemas", annotations=READ_ONLY)
@_engine_tool
async def montycat_list_enforced_schemas(
    keyspace: Optional[str] = None,
    scope: Optional[str] = None,
) -> Any:
    """List schemas enforced on a keyspace, including field data types.

    Use this before a structured write or retrieval when the target keyspace's
    required fields or types are unknown. For retrieval, it helps construct
    correctly typed field filters. This inspection is read-only and never
    creates a missing keyspace.

    Args:
        keyspace: Explicit keyspace name. Takes precedence over scope.
        scope: Owner/user memory scope (maps to its configured keyspace). Use
               "shared" for the common keyspace. When both inputs are omitted,
               the configured default keyspace is inspected.
    """
    name = _resolve_keyspace(scope, keyspace)
    persistent = await _resolve_persistent(name)
    if persistent is None:
        return _failure(f"Keyspace {name!r} does not exist.")
    ks = _keyspace(name, persistent=persistent)
    return await _call(ks.list_all_schemas_in_keyspace())


@mcp.tool(title="View Memory Policy", annotations=READ_ONLY)
@_engine_tool
async def montycat_policy_view(store: Optional[str] = None) -> Any:
    """View the configured owner's effective Montycat governance policy.

    This is read-only. It reports the authenticated owner's effective grants,
    denials, accessible and owned keyspaces, automatic creator capabilities,
    provisioning constraints, and policy health. The engine filters the result
    and remains the authorization boundary.

    Args:
        store: Optional store to inspect. Defaults to the store configured by
               MONTYCAT_URI or MONTYCAT_STORE.
    """
    await _engine_ready()
    engine = _get_engine()
    store = store or engine.store
    return await _call(engine.policy_view(store=store))


@mcp.tool(title="View Policy History", annotations=READ_ONLY)
@_engine_tool
async def montycat_policy_history(
    store: Optional[str] = None,
    keyspace: Optional[str] = None,
) -> Any:
    """View governance history visible to the configured owner.

    This is read-only and owner-scoped by the authenticated Montycat
    credential. It can show when authority was delegated, denied, revoked, or
    transferred without allowing the MCP caller to select another owner.

    Args:
        store: Optional store filter. Defaults to the configured store.
        keyspace: Optional keyspace filter.
    """
    await _engine_ready()
    engine = _get_engine()
    store = store or engine.store
    if keyspace and not store:
        raise ValueError(
            "store is required when filtering policy history by keyspace."
        )
    return await _call(engine.policy_history(store=store, keyspace=keyspace))


@mcp.tool(title="Explain Policy Decision", annotations=READ_ONLY)
@_engine_tool
async def montycat_policy_explain(
    capability: str,
    store: Optional[str] = None,
    keyspace: Optional[str] = None,
    storage: Optional[str] = None,
    semantic_model: Optional[str] = None,
) -> Any:
    """Explain whether the configured owner may perform a proposed action.

    This is a read-only policy check for planning and diagnostics; executing
    the action still requires a separate tool call and fresh engine
    authorization. The explanation identifies applicable grants, denials,
    creator authority, and storage/model constraints.

    Args:
        capability: One of "provision-keyspace", "remove-keyspace",
                    "manage-snapshots", "manage-semantic", "manage-schema",
                    or "manage-access".
        store: Target store. Defaults to the configured store.
        keyspace: Optional target keyspace.
        storage: Optional keyspace type: "persistent", "inmemory", or
                 "distributed".
        semantic_model: Optional model constraint: "minilm", "bge-small",
                        "bge-base", or "e5-small".
    """
    try:
        typed_capability = PolicyCapability(capability)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in PolicyCapability)
        raise ValueError(f"capability must be one of: {allowed}.") from exc

    typed_storage = None
    if storage is not None:
        try:
            typed_storage = PolicyKeyspaceType(storage)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in PolicyKeyspaceType)
            raise ValueError(f"storage must be one of: {allowed}.") from exc

    typed_model = None
    if semantic_model is not None:
        try:
            typed_model = SemanticModel(semantic_model)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in SemanticModel)
            raise ValueError(f"semantic_model must be one of: {allowed}.") from exc

    await _engine_ready()
    engine = _get_engine()
    store = store or engine.store
    if not store:
        raise ValueError(
            "store is required when MONTYCAT_URI/MONTYCAT_STORE does not configure one."
        )
    return await _call(engine.policy_explain(
        capability=typed_capability,
        store=store,
        keyspace=keyspace,
        keyspace_type=typed_storage,
        model=typed_model,
    ))


@mcp.tool(title="Create Memory Keyspace", annotations=MUTATING)
@_engine_tool
async def montycat_create_keyspace(
    keyspace: str,
    storage: Optional[str] = None,
    semantic: bool = False,
    semantic_model: Optional[str] = None,
    persistent: Optional[bool] = None,
    cache: Optional[int] = None,
    compression: bool = False,
) -> Any:
    """Create a new memory namespace using the configured owner's authority.

    A delegated owner can create a keyspace when its governance policy grants
    `provision-keyspace` for the requested store, storage type, and semantic
    model, but its store must already exist. With superowner credentials, the
    engine creates a missing configured store and this first keyspace together
    in the same provisioning request. The engine remains the final
    authorization boundary.

    Args:
        keyspace: Name of the keyspace to create.
        storage: Preferred storage type: "persistent" or "inmemory". Defaults
                 to "persistent".
        semantic: Enable semantic search for this keyspace after creation.
        semantic_model: Optional embedding model: "minilm", "bge-small",
                        "bge-base", or "e5-small". Supplying a model implies
                        semantic=True.
        persistent: Deprecated compatibility option. True maps to
                    storage="persistent"; False maps to storage="inmemory".
        cache: Optional cache size in MB (persistent only; min/default 10).
        compression: Enable compression (persistent only).
    """
    if not isinstance(keyspace, str) or not keyspace.strip():
        raise ValueError("keyspace must be a non-empty string.")

    valid_storage = {"persistent", "inmemory"}
    if storage is not None and storage not in valid_storage:
        raise ValueError('storage must be "persistent" or "inmemory".')
    legacy_storage = None
    if persistent is not None:
        legacy_storage = "persistent" if persistent else "inmemory"
    if storage is not None and legacy_storage is not None and storage != legacy_storage:
        raise ValueError("storage and persistent specify conflicting storage types.")
    storage = storage or legacy_storage or "persistent"
    is_persistent = storage == "persistent"

    if not is_persistent and (cache is not None or compression):
        raise ValueError("cache and compression are supported only for persistent keyspaces.")

    model = None
    if semantic_model is not None:
        try:
            model = SemanticModel(semantic_model)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in SemanticModel)
            raise ValueError(f"semantic_model must be one of: {allowed}.") from exc
        semantic = True

    await _engine_ready()
    ks = _keyspace(keyspace, persistent=is_persistent)
    if is_persistent:
        result = await _call(ks.create_keyspace(
            cache=cache,
            compression=compression,
            semantic=semantic,
        ))
    else:
        result = await _call(ks.create_keyspace(
            semantic=semantic,
        ))
    if isinstance(result, dict) and result.get("status") is False:
        return result

    # Cache only a successful creation. Recording before the engine accepts it
    # makes a later bind believe a nonexistent keyspace already exists.
    _ks_type_cache[keyspace] = is_persistent

    if semantic:
        engine = _get_engine()
        if not engine.store:
            return _failure(
                "Semantic keyspace enablement requires MONTYCAT_STORE or a store in MONTYCAT_URI. "
                f"Keyspace {keyspace!r} was created but semantic search was not enabled."
            )
        semantic_result = await _call(engine.enable_semantic_search(
            model=model, store=engine.store, keyspace=keyspace
        ))
        if isinstance(semantic_result, dict) and semantic_result.get("status") is False:
            return semantic_result
        return {
            "status": True,
            "payload": {
                "keyspace": keyspace,
                "storage": storage,
                "semantic": True,
                "semantic_model": semantic_model,
                "creation": result,
                "semantic_result": semantic_result,
            },
            "error": None,
        }
    return result


@mcp.tool(title="Delete Memory Keyspace", annotations=DESTRUCTIVE)
@_binding_failure
async def montycat_remove_keyspace(
    keyspace: Optional[str] = None,
    scope: Optional[str] = None,
) -> Any:
    """Permanently remove a memory namespace using the owner's authority.

    This is a destructive lifecycle operation. Before removal Montycat MCP closes
    the keyspace's live watch and releases MCP resource-subscription ownership
    so the engine cannot deadlock on a lingering subscriber. The engine then
    enforces `remove-keyspace`, creator authority, and explicit denials.

    Args:
        scope: Owner/user scope to remove (maps to keyspace mem_<scope>).
               Use "shared" for the configured shared keyspace.
        keyspace: Explicit keyspace override (advanced; bypasses scope).
    """
    name = _resolve_keyspace(scope, keyspace)
    if not isinstance(name, str) or not name.strip():
        raise ValueError("keyspace must be a non-empty string.")

    # Never use _bind here: its normal memory behavior auto-provisions a
    # missing keyspace, which would turn "remove absent" into create+remove.
    persistent = await _resolve_persistent(name)
    if persistent is None:
        return _failure(
            f"Keyspace {name!r} does not exist or is not visible to the configured owner."
        )
    ks = _keyspace(name, persistent=persistent)

    # Teardown is load-bearing: an engine subscription left alive can deadlock
    # remove_keyspace. Resource ownership is also discarded so stale sessions
    # cannot retain or later revive the removed keyspace's watch.
    await watch_registry.stop(name)
    uri = _memory_uri(name)
    _resource_sessions.pop(uri, None)

    result = await _call(ks.remove_keyspace())
    if isinstance(result, dict) and result.get("status") is False:
        return result

    _ks_type_cache.pop(name, None)
    _keyspaces.pop((name, True), None)
    _keyspaces.pop((name, False), None)
    return result


@mcp.tool(title="Enable Semantic Search", annotations=MUTATING)
@_engine_tool
async def montycat_enable_semantic(
    keyspace: str,
    store: Optional[str] = None,
    semantic_model: Optional[str] = None,
    field: Optional[str] = None,
) -> Any:
    """Enable semantic search for one explicit keyspace.

    The engine enforces `manage-semantic`, creator authority, explicit denials,
    and allowed-model constraints. Existing records are backfilled by the
    engine. This tool never enables semantic search database-wide.

    Args:
        keyspace: Explicit keyspace to enroll and backfill.
        store: Target store. Defaults to the configured store.
        semantic_model: Optional model: "minilm", "bge-small", "bge-base",
                        or "e5-small". Omit to use the engine/policy default.
        field: Optional JSON field to embed instead of the whole stored value.
    """
    if not isinstance(keyspace, str) or not keyspace.strip():
        raise ValueError("keyspace must be a non-empty string.")
    if field is not None and (not isinstance(field, str) or not field.strip()):
        raise ValueError("field must be a non-empty string when provided.")

    model = None
    if semantic_model is not None:
        try:
            model = SemanticModel(semantic_model)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in SemanticModel)
            raise ValueError(f"semantic_model must be one of: {allowed}.") from exc

    await _engine_ready()
    engine = _get_engine()
    store = store or engine.store
    if not store:
        raise ValueError(
            "store is required when MONTYCAT_URI/MONTYCAT_STORE does not configure one."
        )
    return await _call(engine.enable_semantic_search(
        model=model,
        field=field,
        store=store,
        keyspace=keyspace,
    ))


def _semantic_store(engine: Engine, store: Optional[str]) -> str:
    """Resolve the explicit store required by keyspace-scoped semantic calls."""
    resolved = store or engine.store
    if not resolved:
        raise ValueError(
            "store is required when MONTYCAT_URI/MONTYCAT_STORE does not configure one."
        )
    return resolved


@mcp.tool(title="View Semantic Search Status", annotations=READ_ONLY)
@_engine_tool
async def montycat_semantic_status(
    store: Optional[str] = None,
    keyspace: Optional[str] = None,
) -> Any:
    """Read the engine's actual semantic configuration and backfill state.

    Pass both `store` and `keyspace` for one keyspace. Omitting both asks for
    the database-wide view, which may require superowner authority.
    """
    if keyspace is not None and (not isinstance(keyspace, str) or not keyspace.strip()):
        raise ValueError("keyspace must be a non-empty string when provided.")
    await _engine_ready()
    engine = _get_engine()
    if keyspace is not None:
        store = _semantic_store(engine, store)
    return await _call(engine.get_semantic_status(store=store, keyspace=keyspace))


@mcp.tool(title="Enable External Vectors", annotations=MUTATING)
@_engine_tool
async def montycat_enable_external_vectors(
    keyspace: str,
    dimensions: int,
    embedding_space: str,
    store: Optional[str] = None,
) -> Any:
    """Enroll a keyspace for caller-supplied embeddings instead of text embedding."""
    if not isinstance(keyspace, str) or not keyspace.strip():
        raise ValueError("keyspace must be a non-empty string.")
    if not isinstance(dimensions, int) or isinstance(dimensions, bool) or not 1 <= dimensions <= 4096:
        raise ValueError("dimensions must be an integer between 1 and 4096.")
    if not isinstance(embedding_space, str) or not 1 <= len(embedding_space) <= 128:
        raise ValueError("embedding_space must contain 1 to 128 characters.")
    await _engine_ready()
    engine = _get_engine()
    store = _semantic_store(engine, store)
    return await _call(engine.enable_precomputed_vector_search(
        store, keyspace, dimensions, embedding_space
    ))


@mcp.tool(title="Rebuild Semantic Vectors", annotations=DESTRUCTIVE)
@_engine_tool
async def montycat_reembed_semantic(
    keyspace: str,
    semantic_model: str,
    store: Optional[str] = None,
    field: Optional[str] = None,
) -> Any:
    """Replace an enrolled keyspace's text embedding model and backfill it.

    This clears its current vectors, then has the engine rebuild them. Use
    `montycat_semantic_status` to observe the resulting configuration.
    """
    if not isinstance(keyspace, str) or not keyspace.strip():
        raise ValueError("keyspace must be a non-empty string.")
    if field is not None and (not isinstance(field, str) or not field.strip()):
        raise ValueError("field must be a non-empty string when provided.")
    try:
        model = SemanticModel(semantic_model)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in SemanticModel)
        raise ValueError(f"semantic_model must be one of: {allowed}.") from exc
    await _engine_ready()
    engine = _get_engine()
    store = _semantic_store(engine, store)
    return await _call(engine.reembed_semantic_search(
        model, store, keyspace, field=field
    ))


@mcp.tool(title="Disable Semantic Search", annotations=DESTRUCTIVE)
@_engine_tool
async def montycat_disable_semantic(
    keyspace: str,
    store: Optional[str] = None,
    drop_vectors: bool = False,
) -> Any:
    """Disable semantic search for one explicit keyspace.

    Stored vectors are retained by default so re-enabling can resume without a
    full rebuild. Set `drop_vectors` only when intentionally clearing vectors,
    such as before changing embedding models. The engine enforces all
    governance authority and explicit denials.

    Args:
        keyspace: Explicit keyspace to unenroll.
        store: Target store. Defaults to the configured store.
        drop_vectors: Also delete stored vectors for this keyspace.
    """
    if not isinstance(keyspace, str) or not keyspace.strip():
        raise ValueError("keyspace must be a non-empty string.")

    await _engine_ready()
    engine = _get_engine()
    store = store or engine.store
    if not store:
        raise ValueError(
            "store is required when MONTYCAT_URI/MONTYCAT_STORE does not configure one."
        )
    return await _call(engine.disable_semantic_search(
        drop_vectors=drop_vectors,
        store=store,
        keyspace=keyspace,
    ))


async def _snapshot_keyspace(keyspace: str):
    """Resolve an existing in-memory keyspace without auto-provisioning."""
    if not isinstance(keyspace, str) or not keyspace.strip():
        raise ValueError("keyspace must be a non-empty string.")
    persistent = await _resolve_persistent(keyspace)
    if persistent is None:
        raise KeyspaceBindingError(
            f"Keyspace {keyspace!r} does not exist or is not visible to the configured owner."
        )
    if persistent:
        raise ValueError("Snapshots are supported only for in-memory keyspaces.")
    return _keyspace(keyspace, persistent=False)


@mcp.tool(title="Start Memory Snapshots", annotations=MUTATING)
@_binding_failure
async def montycat_start_snapshots(keyspace: str) -> Any:
    """Start scheduled snapshots for one existing in-memory keyspace.

    Montycat enforces `manage-snapshots`, creator authority, and explicit
    denials. If the response says "Snapshot rate is not set", snapshot
    scheduling is not configured on the engine; that is an environmental
    configuration error, not an authorization denial. This tool cannot alter
    the global snapshot rate.

    Args:
        keyspace: Explicit in-memory keyspace to snapshot.
    """
    ks = await _snapshot_keyspace(keyspace)
    # The method name is misspelled in the current Python SDK; keep that detail
    # contained at this adapter boundary.
    return await _call(ks.do_snaphots_for_keyspace())


@mcp.tool(title="Stop Memory Snapshots", annotations=MUTATING)
@_binding_failure
async def montycat_stop_snapshots(keyspace: str) -> Any:
    """Stop scheduled snapshots for one existing in-memory keyspace.

    Existing snapshot files are retained. Montycat performs the final
    authorization check.

    Args:
        keyspace: Explicit in-memory keyspace whose snapshot schedule stops.
    """
    ks = await _snapshot_keyspace(keyspace)
    return await _call(ks.stop_snapshots_for_keyspace())


@mcp.tool(title="Delete Memory Snapshots", annotations=DESTRUCTIVE)
@_binding_failure
async def montycat_clean_snapshots(keyspace: str) -> Any:
    """Delete snapshot files for one existing in-memory keyspace.

    This is destructive to the keyspace's snapshot history but does not delete
    its currently loaded in-memory records. Montycat performs the final
    authorization check.

    Args:
        keyspace: Explicit in-memory keyspace whose snapshots are cleaned.
    """
    ks = await _snapshot_keyspace(keyspace)
    return await _call(ks.clean_snapshots_for_keyspace())


@mcp.tool(title="Delete Memory", annotations=DESTRUCTIVE)
@_binding_failure
async def montycat_forget(
    keyspace: Optional[str] = None,
    scope: Optional[str] = None,
    key: Optional[str] = None,
    custom_key: Optional[str] = None,
    wait_for_index: Optional[bool] = None,
) -> Any:
    """Delete a stored record from memory by key or custom key.

    Args:
        keyspace: Memory namespace (defaults to the configured one).
        key: Montycat-generated key to delete.
        custom_key: Custom key to delete.
        wait_for_index: For persistent keyspaces, wait for secondary indexes
                        before returning. Defaults to the engine setting.
    """
    if key is None and custom_key is None:
        raise ValueError("Provide one of: key or custom_key.")
    ks = await _bind(_resolve_keyspace(scope, keyspace))
    return await _call(ks.delete_key(key=key, custom_key=custom_key, wait_for_index=wait_for_index))


@mcp.tool(title="Update Memory", annotations=MUTATING)
@_binding_failure
async def montycat_update(
    updates: dict,
    keyspace: Optional[str] = None,
    scope: Optional[str] = None,
    key: Optional[str] = None,
    custom_key: Optional[str] = None,
    wait_for_index: Optional[bool] = None,
    vector: Optional[list[float]] = None,
) -> Any:
    """Revise an existing memory in place (memory is mutable).

    Use this when a stored fact changes — a corrected value, an updated
    preference — instead of storing a duplicate. Only the fields you pass are
    changed. Identify the record by `key` or `custom_key`.

    Args:
        updates: Fields to change, e.g. {"status": "resolved"} or {"name": "Alice"}.
        keyspace: Memory namespace (defaults to the configured one).
        key: Montycat-generated key of the record to update.
        custom_key: Custom key of the record to update.
        wait_for_index: For persistent keyspaces, wait for secondary indexes
                        before returning. Defaults to the engine setting.
    """
    if key is None and custom_key is None:
        raise ValueError("Provide one of: key or custom_key.")
    if not updates:
        raise ValueError("updates must be a non-empty JSON object.")
    ks = await _bind(_resolve_keyspace(scope, keyspace))
    return await _call(ks.update_value(
        key=key, custom_key=custom_key, vector=vector,
        wait_for_index=wait_for_index, **updates
    ))


@mcp.tool(title="List Memories", annotations=READ_ONLY)
@_binding_failure
async def montycat_list_memories(
    keyspace: Optional[str] = None,
    scope: Optional[str] = None,
    limit: int = 25,
    recent: bool = True,
) -> Any:
    """Browse stored memories — enumerate what is remembered, not search by meaning.

    Returns up to `limit` records with their keys. Use this to review or list
    memory; for meaning-based recall use montycat_semantic_search, and for exact
    lookups use montycat_recall.

    Args:
        keyspace: Memory namespace (defaults to the configured one).
        limit: Max records to return (default 25).
        recent: Return the most recently written records first (default True).
                Persistent keyspaces order by key, which is a strict write
                order; in-memory keyspaces have no ordered read, so there the
                bias stays approximate (by storage volume) and falls back to a
                full scan when the latest volume is empty. Pass False to read
                from the oldest record forward.
    """
    _validate_limit(limit)
    name = _resolve_keyspace(scope, keyspace)
    ks = await _bind(name)
    # get_keys needs a volume selector *or* a range; `latest_volume=False` alone
    # is neither, and the client rejects it with "Please provide volumes/latest
    # volume or limit." Only the persistent client takes a range and an `order`
    # (engine >= 1.3.1): the in-memory one is get_keys(volumes, latest_volume)
    # with no `limit` parameter at all, so handing it one raises TypeError
    # instead of returning keys. Engine 1.3.1 made the range half-open, so
    # [0, limit] is exactly `limit` keys (verified live: [0, 2] returns two);
    # the slice below stays as a guard for engines that read it inclusively.
    is_persistent = issubclass(ks, Keyspace.Persistent)

    async def _scan_widest():
        """Read the whole keyspace, however its storage type allows it."""
        if is_persistent:
            return await _call(ks.get_keys(limit=[0, limit], order=ResultOrder.ASCENDING))
        volumes = await _inmemory_volumes(name)
        if not volumes:
            return {"status": True, "payload": [], "error": None}
        return await _call(ks.get_keys(volumes=volumes))

    if recent and is_persistent:
        # A descending key range is the whole keyspace read newest-first, so
        # there is no empty-latest-volume case to widen out of below.
        keys_res = await _call(ks.get_keys(limit=[0, limit], order=ResultOrder.DESCENDING))
    elif recent:
        keys_res = await _call(ks.get_keys(latest_volume=True))
    else:
        keys_res = await _scan_widest()
    if isinstance(keys_res, dict) and keys_res.get("status") is False:
        return keys_res  # surface the failure instead of reporting "no memories"
    keys = keys_res.get("payload") if isinstance(keys_res, dict) else None
    if not keys and recent and not is_persistent:
        # The latest volume can be empty while older volumes hold records, and
        # an empty payload is indistinguishable from "nothing remembered" once
        # it reaches the caller. Widen to a full scan before reporting the
        # keyspace as empty, so a populated keyspace is never listed as bare.
        keys_res = await _scan_widest()
        if isinstance(keys_res, dict) and keys_res.get("status") is False:
            return keys_res
        keys = keys_res.get("payload") if isinstance(keys_res, dict) else None
    if not keys:
        return {"status": True, "payload": [], "error": None}
    keys = list(keys)[:limit]
    return await _call(ks.get_bulk(bulk_keys=keys, key_included=True))


@mcp.tool(title="Store Multiple Memories", annotations=MUTATING)
@_binding_failure
async def montycat_remember_bulk(
    values: list,
    keyspace: Optional[str] = None,
    scope: Optional[str] = None,
    timestamp: Optional[bool] = None,
    wait_for_index: Optional[bool] = None,
    vectors: Optional[list[list[float]]] = None,
) -> Any:
    """Store many memories at once; all are embedded and indexed automatically.

    Args:
        values: A list of records (JSON objects) to store.
        keyspace: Memory namespace (defaults to the configured one).
        timestamp: Index a `_created_at` on each record for time-range recall.
                   Defaults to MONTYCAT_AUTO_TIMESTAMP (on). Pass False for
                   large imports that will never be recalled by time — it skips
                   a server-side timestamp parse per record.
        wait_for_index: For persistent keyspaces, wait for secondary indexes
                        before returning. Defaults to the engine setting.
    """
    if not values or not all(isinstance(value, dict) and value for value in values):
        raise ValueError("values must be a non-empty list of non-empty JSON objects.")
    if vectors is not None and len(vectors) != len(values):
        raise ValueError("vectors must contain one embedding for every value.")
    ks = await _bind(_resolve_keyspace(scope, keyspace))
    values = [_stamp(value, timestamp) for value in values]
    return await _call(ks.insert_bulk(
        bulk_values=values, vectors=vectors, wait_for_index=wait_for_index
    ))


@mcp.tool(title="Wait for Memory Change", annotations=READ_ONLY)
@_binding_failure
async def montycat_await_memory_change(
    keyspace: Optional[str] = None,
    scope: Optional[str] = None,
    timeout_sec: int = 30,
    since_seq: Optional[int] = None,
) -> Any:
    """Wait until memory CHANGES — returns the moment another agent or session
    writes, updates, or deletes something in this memory.

    This is a live subscription to the database, not a poll: it sleeps until a
    change actually happens and then returns immediately. Use it to coordinate
    with other agents sharing a scope ("tell me when someone adds to our shared
    memory"), or to confirm a write from another session landed. Do NOT call it
    in a tight loop as a substitute for searching — to *find* things, use
    montycat_semantic_search.

    Returns `{changes: [...], next_seq, oldest_seq, cursor_expired, timed_out}`.
    Each change is
    `{seq, key, event, value}` where event is "inserted" (covers create and
    update) or "removed". Pass the returned `next_seq` back as `since_seq` on
    the next call to resume exactly where you left off. If the bounded buffer
    has discarded part of that history, `cursor_expired` is true and
    `oldest_seq` identifies the earliest retained record.

    Args:
        scope: Owner/user id whose memory to watch (keyspace mem_<scope>).
               Use "shared" for the common keyspace — the usual choice when
               coordinating between agents.
        keyspace: Explicit keyspace override (advanced; bypasses scope).
        timeout_sec: How long to wait before giving up (default 30). On timeout
                     the result is empty with `timed_out: true` — that is a
                     normal outcome, not an error.
        since_seq: Resume cursor from a previous call. Omit on the first call to
                   watch only for changes from now on.
    """
    name = _resolve_keyspace(scope, keyspace)
    ks = await _bind(name)
    watch = await watch_registry.get_or_start(
        name, ks, authorize=lambda: _authorize_watch(name)
    )

    if watch.revoked_error is not None:
        reason = watch.revoked_error
        await watch_registry.stop(name)
        return _failure(
            f"Memory watch access was revoked or could not be revalidated: {reason}. "
            "Buffered changes were purged."
        )

    # A subscription that never connected would otherwise wait out the full
    # timeout and report `timed_out: true` — indistinguishable from "nothing
    # changed", which is a lie the agent cannot detect. Fail loudly instead.
    problem = await watch.ensure_established()
    if problem is not None:
        await watch_registry.stop(name)
        return {
            "status": False,
            "payload": None,
            "error": (
                f"{problem}. Real-time watch needs the engine's subscription "
                f"server (default port {_engine_port() + 1}); check it is "
                f"reachable and that subscriptions are allowed."
            ),
        }

    timeout = max(1, min(int(timeout_sec), 300))
    # The first call begins at this exact boundary. Passing the captured cursor
    # to wait prevents a change between subscription setup and waiter creation
    # from being silently dropped.
    baseline_seq = watch.seq if since_seq is None else since_seq
    cursor_expired = watch.cursor_expired(since_seq)
    changes = await watch.wait(baseline_seq, timeout=timeout)
    if watch.revoked_error is not None:
        reason = watch.revoked_error
        await watch_registry.stop(name)
        return _failure(
            f"Memory watch access was revoked or could not be revalidated: {reason}. "
            "Buffered changes were purged."
        )
    await watch_registry.reap_idle()

    return {
        "status": True,
        "payload": {
            "keyspace": name,
            "changes": changes,
            "next_seq": watch.seq,
            "oldest_seq": watch.oldest_seq,
            "cursor_expired": cursor_expired,
            "timed_out": not changes,
        },
        "error": None,
    }


# ── Surface A: MCP resources + resources/updated push ────────────────────────
#
# The tool above works in every client today. This second surface is the
# spec-correct one: a client subscribes to a memory resource and the server
# pushes `notifications/resources/updated` when it changes. Both read the same
# subscription, so a change is delivered once and seen by both.

_MEMORY_URI = "montycat://memory/{keyspace}"
_LEGACY_MEMORY_URI = "memocat://memory/{keyspace}"


def _memory_uri(keyspace: str) -> str:
    return _MEMORY_URI.format(keyspace=keyspace)


async def _authorize_watch(keyspace: str) -> Optional[str]:
    """Revalidate that the authenticated engine can still see a keyspace.

    Structure discovery is server-filtered by effective read authority and
    avoids fetching memory values. It is intentionally uncached for leases.
    """
    await _engine_ready()
    result = await _call(_get_engine().get_structure_available())
    if isinstance(result, dict) and result.get("status") is False:
        return result.get("error") or "policy revalidation failed"
    payload = result.get("payload") if isinstance(result, dict) else None
    structure = (payload or {}).get("structure") or {}
    for store in structure.values():
        if not isinstance(store, dict):
            continue
        if keyspace in (store.get("persistent") or {}):
            return None
        if keyspace in (store.get("inmemory") or {}):
            return None
    return f"read authority no longer includes keyspace {keyspace!r}"


@mcp.resource(_LEGACY_MEMORY_URI, mime_type="application/json")
@mcp.resource(_MEMORY_URI, mime_type="application/json")
async def memory_resource(keyspace: str) -> Any:
    """A memory namespace, readable as a resource and subscribable for live
    change notifications."""
    try:
        ks = await _bind(keyspace)
    except KeyspaceBindingError as exc:
        return _failure(str(exc))
    res = await _call(ks.get_len())
    if isinstance(res, dict) and res.get("status") is False:
        return res
    count = res.get("payload") if isinstance(res, dict) else None
    watch = watch_registry.get(keyspace)
    return {
        "keyspace": keyspace,
        "memories": count,
        "watching": bool(watch and watch.running),
        "last_change_seq": watch.seq if watch else 0,
    }


# Background notifications happen outside a request, so preserve ownership by
# resource URI and session. A single global session worked only for stdio and
# would send every HTTP client's update to whichever client subscribed last.
_resource_sessions: dict[str, dict[int, Any]] = {}


def _request_session() -> Any:
    try:
        return mcp._mcp_server.request_context.session
    except LookupError:
        return None


def _add_resource_session(uri: str, session: Any) -> None:
    _resource_sessions.setdefault(uri, {})[id(session)] = session


def _remove_resource_session(uri: str, session: Any) -> bool:
    sessions = _resource_sessions.get(uri)
    if sessions is None:
        return False
    sessions.pop(id(session), None)
    if sessions:
        return False
    _resource_sessions.pop(uri, None)
    return True


async def _release_resource_subscription(uri: str, session: Any) -> None:
    """Drop one client's ownership and stop the engine watch when it was last."""
    if not _remove_resource_session(uri, session):
        return
    name = _keyspace_from_uri(uri)
    watch = watch_registry.get(name) if name else None
    if watch is None:
        return
    watch.resource_uris.discard(uri)
    if not watch.in_use():
        await watch_registry.stop(name)


async def _send_resource_updated(uri: str, session: Any) -> None:
    from pydantic import AnyUrl

    try:
        await session.send_resource_updated(AnyUrl(uri))
    except Exception:
        # A disconnected HTTP client might not send unsubscribe. Treat a failed
        # notification as release so it cannot keep an engine subscription alive.
        await _release_resource_subscription(uri, session)


def _notify_resource_updated(watch, _record: dict) -> None:
    """Bridge callback: a change landed, tell subscribed MCP clients.

    Runs inline in the subscription read loop, so it only schedules the send —
    it never awaits it.
    """
    if not watch.resource_uris:
        return

    for uri in list(watch.resource_uris):
        for session in list(_resource_sessions.get(uri, {}).values()):
            try:
                asyncio.get_running_loop().create_task(_send_resource_updated(uri, session))
            except Exception:
                pass


watch_registry.on_change = _notify_resource_updated


def _watch_revoked(watch, _reason: str) -> None:
    """Discard resource ownership immediately when a lease loses access."""
    for uri in list(watch.resource_uris):
        _resource_sessions.pop(uri, None)
    watch.resource_uris.clear()


watch_registry.on_revoke = _watch_revoked


def _keyspace_from_uri(uri: str) -> Optional[str]:
    text = str(uri)
    for prefix in ("montycat://memory/", "memocat://memory/"):
        if text.startswith(prefix):
            return text[len(prefix):] or None
    return None


@mcp._mcp_server.subscribe_resource()
async def _subscribe_resource(uri) -> None:
    """A client subscribed to a memory resource — open the live subscription."""
    name = _keyspace_from_uri(uri)
    session = _request_session()
    if not name or session is None:
        return
    ks = await _bind(name)
    watch = await watch_registry.get_or_start(
        name, ks, authorize=lambda: _authorize_watch(name)
    )
    if watch.revoked_error is not None:
        await watch_registry.stop(name)
        raise PermissionError(watch.revoked_error)
    problem = await watch.ensure_established()
    if problem is not None:
        await watch_registry.stop(name)
        raise PermissionError(
            f"Resource subscription rejected for keyspace {name!r}: {problem}"
        )
    text_uri = str(uri)
    watch.resource_uris.add(text_uri)
    _add_resource_session(text_uri, session)


@mcp._mcp_server.unsubscribe_resource()
async def _unsubscribe_resource(uri) -> None:
    """Last subscriber gone — release the engine subscription (a lingering one
    blocks later keyspace removal; see watch.py)."""
    session = _request_session()
    if session is None:
        return
    await _release_resource_subscription(str(uri), session)


async def _run_stdio() -> None:
    """Run the server over stdio, advertising resource subscriptions.

    `mcp.run()` cannot be used: the SDK hardcodes `subscribe=False` in the
    resources capability (mcp/server/lowlevel/server.py) even when subscribe
    handlers are registered, so a compliant client would never subscribe and
    the push surface would be dead. Everything else is the SDK's own stdio
    path. If a future SDK exposes the capability properly, this collapses back
    to `mcp.run()`.
    """
    from mcp.server.lowlevel.server import NotificationOptions
    from mcp.server.stdio import stdio_server

    options = mcp._mcp_server.create_initialization_options(
        NotificationOptions(resources_changed=True)
    )
    if options.capabilities.resources is not None:
        options.capabilities.resources.subscribe = True

    async with stdio_server() as (read_stream, write_stream):
        # Acquire the engine *behind* the open transport, never in front of it.
        # Connecting to a running engine is instant, but starting one can pull a
        # container image or download an embedding model, and a client that is
        # still waiting to complete `initialize` gives up long before that.
        # Tools report progress through `_engine_ready` instead. Logs go to
        # stderr — stdout is the transport and writing there corrupts it.
        start_bootstrap()
        try:
            await mcp._mcp_server.run(read_stream, write_stream, options)
        finally:
            _resource_sessions.clear()
            await watch_registry.stop_all()
            if _bootstrap_task is not None and not _bootstrap_task.done():
                _bootstrap_task.cancel()
                with suppress(asyncio.CancelledError):
                    await _bootstrap_task


# Deprecated Python-call aliases. They are intentionally not registered as MCP
# tools: the 1.0 tool catalog advertises only the canonical Montycat names.
memocat_semantic_search = montycat_semantic_search
memocat_remember = montycat_remember
memocat_recall = montycat_recall
memocat_install_engine = montycat_install_engine
memocat_list_keyspaces = montycat_list_keyspaces
memocat_list_enforced_schemas = montycat_list_enforced_schemas
memocat_policy_view = montycat_policy_view
memocat_policy_history = montycat_policy_history
memocat_policy_explain = montycat_policy_explain
memocat_create_keyspace = montycat_create_keyspace
memocat_remove_keyspace = montycat_remove_keyspace
memocat_enable_semantic = montycat_enable_semantic
memocat_semantic_status = montycat_semantic_status
memocat_enable_external_vectors = montycat_enable_external_vectors
memocat_reembed_semantic = montycat_reembed_semantic
memocat_disable_semantic = montycat_disable_semantic
memocat_start_snapshots = montycat_start_snapshots
memocat_stop_snapshots = montycat_stop_snapshots
memocat_clean_snapshots = montycat_clean_snapshots
memocat_forget = montycat_forget
memocat_update = montycat_update
memocat_list_memories = montycat_list_memories
memocat_remember_bulk = montycat_remember_bulk
memocat_await_memory_change = montycat_await_memory_change


def main() -> None:
    """Console entry point — runs the MCP server over stdio."""
    asyncio.run(_run_stdio())


if __name__ == "__main__":
    main()
