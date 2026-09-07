"""Directory-facing MCP tool metadata must remain complete and explicit."""

import pytest

from montycat_mcp.server import SERVER_INSTRUCTIONS, mcp


READ_ONLY = {
    "montycat_semantic_search",
    "montycat_recall",
    "montycat_list_memories",
    "montycat_list_keyspaces",
    "montycat_list_enforced_schemas",
    "montycat_semantic_status",
    "montycat_policy_view",
    "montycat_policy_history",
    "montycat_policy_explain",
    "montycat_await_memory_change",
}

DESTRUCTIVE = {
    "montycat_forget",
    "montycat_remove_keyspace",
    "montycat_clean_snapshots",
    "montycat_reembed_semantic",
    "montycat_disable_semantic",
    # Installs system-wide software behind an administrator prompt. 
    "montycat_install_engine",
}

NON_DESTRUCTIVE_MUTATING = {
    "montycat_remember",
    "montycat_remember_bulk",
    "montycat_update",
    "montycat_create_keyspace",
    "montycat_enable_semantic",
    "montycat_enable_external_vectors",
    "montycat_start_snapshots",
    "montycat_stop_snapshots",
}


@pytest.mark.asyncio
async def test_all_tools_have_directory_metadata():
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert set(tools) == READ_ONLY | NON_DESTRUCTIVE_MUTATING | DESTRUCTIVE
    assert len(tools) == 24

    for name, tool in tools.items():
        assert tool.title, f"{name} must have a user-facing title"
        assert tool.annotations is not None, f"{name} must have safety annotations"
        assert tool.annotations.readOnlyHint is not None
        assert tool.annotations.destructiveHint is not None
        assert tool.annotations.idempotentHint is not None
        assert tool.annotations.openWorldHint is not None

    for name in READ_ONLY:
        assert tools[name].annotations.readOnlyHint is True
        assert tools[name].annotations.destructiveHint is False

    for name in NON_DESTRUCTIVE_MUTATING:
        assert tools[name].annotations.readOnlyHint is False
        assert tools[name].annotations.destructiveHint is False

    for name in DESTRUCTIVE:
        assert tools[name].annotations.readOnlyHint is False
        assert tools[name].annotations.destructiveHint is True


def test_server_explains_safe_shared_memory_behavior_to_mcp_hosts():
    assert mcp._mcp_server.instructions == SERVER_INSTRUCTIONS
    assert "shared, persistent memory" in SERVER_INSTRUCTIONS
    assert "same Montycat engine" in SERVER_INSTRUCTIONS
    assert "montycat_list_enforced_schemas" in SERVER_INSTRUCTIONS
    assert "write or retrieval" in SERVER_INSTRUCTIONS
    assert "Do not store secrets" in SERVER_INSTRUCTIONS
