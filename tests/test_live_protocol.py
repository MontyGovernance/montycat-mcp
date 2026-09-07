"""Drive the server over a real MCP stdio session, the way a client would.

Covers what in-process calls cannot: the capability handshake, tool listing,
resource templates, and the subscribe/unsubscribe round trip.
"""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager

import pytest

from .conftest import MONTYCAT_URI, requires_engine

pytestmark = [pytest.mark.asyncio, requires_engine]

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEYSPACE = "montycat_t_protocol"


@asynccontextmanager
async def mcp_session():
    """A live stdio session.

    Deliberately not a pytest fixture: `stdio_client` opens an anyio cancel
    scope, and a yield-fixture would exit it from a different task than it was
    entered in ("Attempted to exit cancel scope in a different task"). Entering
    and exiting inside the test body keeps it on one task.
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = dict(os.environ)
    env["MONTYCAT_URI"] = MONTYCAT_URI
    env["MONTYCAT_DEFAULT_KEYSPACE"] = KEYSPACE

    params = StdioServerParameters(
        command=sys.executable,
        # Exercise the exact script named by manifest.json, not merely the
        # underlying import that the normal console entry point also uses.
        args=["mcpb_entry.py"],
        env=env,
        cwd=REPO_ROOT,
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            init = await client.initialize()
            yield client, init


async def test_advertises_resource_subscription():
    """The SDK hardcodes `subscribe=False` even with handlers registered, so
    the server builds its own InitializationOptions. If that override ever
    stops applying, push dies silently — this is the guard."""
    async with mcp_session() as (_, init):
        assert init.serverInfo.name == "Montycat MCP"
        resources = init.capabilities.resources
        assert resources is not None
        assert resources.subscribe is True
        assert init.instructions is not None
        assert "shared, persistent memory" in init.instructions


async def test_tools_are_listed_under_the_montycat_name():
    async with mcp_session() as (client, _):
        tools = (await client.list_tools()).tools
        names = [t.name for t in tools]
        assert "montycat_semantic_search" in names
        assert "montycat_await_memory_change" in names
        assert "montycat_policy_view" in names
        assert "montycat_policy_explain" in names
        assert "montycat_policy_history" in names
        assert "montycat_remove_keyspace" in names
        assert "montycat_enable_semantic" in names
        assert "montycat_disable_semantic" in names
        assert "montycat_start_snapshots" in names
        assert "montycat_stop_snapshots" in names
        assert "montycat_clean_snapshots" in names
        policy_view = next(t for t in tools if t.name == "montycat_policy_view")
        policy_history = next(t for t in tools if t.name == "montycat_policy_history")
        assert "owner" not in policy_view.inputSchema.get("properties", {})
        assert "owner" not in policy_history.inputSchema.get("properties", {})
        assert not any(n.startswith("memocat_") for n in names), \
            "leftover pre-rename tool names"


async def test_memory_resource_template_registered():
    async with mcp_session() as (client, _):
        templates = (await client.list_resource_templates()).resourceTemplates
        assert any("montycat://memory/" in t.uriTemplate for t in templates)


async def test_resource_is_readable_and_subscribable():
    async with mcp_session() as (client, _):
        await client.call_tool("montycat_create_keyspace",
                               {"keyspace": KEYSPACE, "persistent": True})
        uri = f"montycat://memory/{KEYSPACE}"

        contents = (await client.read_resource(uri)).contents
        assert KEYSPACE in (contents[0].text if contents else "")

        await client.subscribe_resource(uri)
        await client.call_tool("montycat_remember",
                               {"value": {"text": "protocol level push"},
                                "keyspace": KEYSPACE})
        result = await client.call_tool("montycat_await_memory_change",
                                        {"keyspace": KEYSPACE, "timeout_sec": 10,
                                         "since_seq": 0})
        text = result.content[0].text if result.content else ""
        assert "protocol level push" in text

        await client.unsubscribe_resource(uri)
        # Server must stay responsive after releasing the subscription.
        assert await client.call_tool("montycat_list_keyspaces", {}) is not None
