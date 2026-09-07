"""MCPB metadata that can be verified before the final artwork is supplied."""

import json
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_mcpb_manifest_matches_project_and_bundle_files():
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert manifest["manifest_version"] == "0.4"
    assert manifest["name"] == "io.github.montygovernance.montycat-mcp"
    assert manifest["version"] == project["project"]["version"]
    assert manifest["server"]["type"] == "uv"
    assert (ROOT / manifest["server"]["entry_point"]).is_file()
    assert (ROOT / manifest["icon"]).is_file()
    assert (ROOT / "PRIVACY.md").is_file()
    assert manifest["privacy_policies"]
    assert project["project"]["name"] == "montycat-mcp"
    assert project["project"]["scripts"]["montycat-mcp"] == "montycat_mcp.server:main"
    assert project["project"]["scripts"]["memocat-mcp"] == "montycat_mcp.server:main"


def test_legacy_distribution_tracks_primary_version():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    legacy = tomllib.loads(
        (ROOT / "compat/memocat-mcp/pyproject.toml").read_text(encoding="utf-8")
    )

    version = project["project"]["version"]
    assert legacy["project"]["version"] == version
    assert legacy["project"]["dependencies"] == [f"montycat-mcp=={version}"]


def test_legacy_python_namespace_aliases_canonical_modules():
    import memocat_mcp.server as legacy
    import montycat_mcp.server as canonical

    assert legacy is canonical
    assert legacy.memocat_remember is canonical.montycat_remember


def test_manifest_declares_exactly_the_runtime_tools():
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    declared = {tool["name"] for tool in manifest["tools"]}

    # Import locally so this file remains a cheap metadata test at collection.
    from montycat_mcp.server import mcp

    runtime = set(mcp._tool_manager._tools)
    assert declared == runtime


def test_runtime_advertises_the_product_display_name():
    from montycat_mcp.server import mcp

    assert mcp.name == "Montycat MCP"


def test_public_metadata_positions_montycat_as_cross_system_shared_memory():
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    registry = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    # One tagline across every public surface, so search and recall reinforce.
    tagline = "Shared, persistent memory for AI agents"
    assert "Shared Memory for AI Agents" in manifest["display_name"]
    assert "Shared Memory for AI Agents" in registry["title"]
    assert manifest["description"].startswith(tagline)
    assert registry["description"].startswith(tagline)
    assert project["project"]["description"].startswith(tagline)

    keywords = {keyword.lower() for keyword in manifest["keywords"]}
    assert {
        "shared memory",
        "persistent memory",
        "multi-agent memory",
        "cross-session memory",
        "mcp memory",
        "claude memory",
        "openai codex",
        "cursor",
    } <= keywords
