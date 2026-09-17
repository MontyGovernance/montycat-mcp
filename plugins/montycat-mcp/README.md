# Montycat MCP Plugin for Claude Code and Claude Cowork

This plugin connects Claude Code and Claude Cowork to the Montycat MCP server.
Montycat MCP gives Claude shared, persistent memory across conversations and AI
agents, with semantic search, real-time change notifications, and governed
keyspaces backed by Montycat.

> Using Claude Desktop? Download the latest `.mcpb` extension from [GitHub
> Releases](https://github.com/MontyGovernance/montycat-mcp/releases/latest).

## Install

Add the MontyGovernance marketplace and install the plugin from Claude Code:

```text
/plugin marketplace add MontyGovernance/montycat-mcp
/plugin install montycat-mcp@montygovernance
```

## Prerequisite: install uv

Montycat MCP is a Python MCP server. Install `uv`, which provides the `uvx`
command used by this plugin:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Restart Claude after installing `uv` so the application receives the updated
`PATH`.

## Configure the Montycat engine

By default, Montycat MCP discovers or starts a supported local Montycat Semantic
engine. Docker may be used when no supported native engine is available.

To connect to an existing engine, set these variables before starting Claude:

```bash
export MONTYCAT_URI="montycat://user:password@host:21210/store"
export MONTYCAT_TLS="true"
export MONTYCAT_TLS_CERTIFICATE_PATH="/path/to/server.crt"
```

Do not commit a URI containing credentials. Use a least-privilege delegated
owner for shared or remote engines.

After installation, run `/mcp` and confirm that `montycat` is connected. If it
is not, verify that `uvx` is available in the environment used to launch
Claude.

## Engine installation permission

The `montycat_install_engine` tool can download an operating-system package and
open the installer. The installer may request administrator approval. Montycat
MCP only performs this operation when that tool is explicitly invoked; ordinary
memory operations do not silently open an installer.

Memory records and embeddings remain in the configured Montycat engine until
they are explicitly deleted. Removing this plugin does not delete that data.
See the full [privacy policy](https://github.com/MontyGovernance/montycat-mcp/blob/master/PRIVACY.md).

## Documentation and support

- [Claude Desktop downloads](https://github.com/MontyGovernance/montycat-mcp/releases/latest)
- [Montycat MCP documentation](https://github.com/MontyGovernance/montycat-mcp#readme)
- [Release notes](https://github.com/MontyGovernance/montycat-mcp/blob/master/CHANGELOG.md)
- [Report an issue](https://github.com/MontyGovernance/montycat-mcp/issues)
