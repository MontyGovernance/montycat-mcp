<img src="https://raw.githubusercontent.com/MontyGovernance/montycat-mcp/master/assets/icon.png" alt="Montycat logo" width="72" align="left" hspace="12"> 

# Montycat MCP - Shared Memory for AI Agents

**A self-hosted MCP server that gives AI agents persistent, searchable memory.**
Claude, Codex, Cursor, and any Model Context Protocol client write to one
memory and read each other's.

[![PyPI](https://img.shields.io/pypi/v/montycat-mcp.svg)](https://pypi.org/project/montycat-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/montycat-mcp.svg)](https://pypi.org/project/montycat-mcp/)
[![License](https://img.shields.io/github/license/MontyGovernance/montycat-mcp.svg)](LICENSE)

<!-- mcp-name: io.github.MontyGovernance/montycat-mcp -->

- **Memory that survives the chat.** Decisions, preferences, and project context carry into the next session.
- **One memory, many agents.** Every MCP client you use works from the same facts.
- **Recall by meaning, keyword, or both.** Vector search finds a memory when the wording differs, BM25 nails exact identifiers, and hybrid mode fuses the two. Exact-key and metadata lookup too.
- **Yours.** Server, engine, and embeddings run on your machine. No hosted memory service, no cloud embedding API.

## Install

**Claude Desktop** — download
[`montycat-mcp.mcpb`](https://github.com/MontyGovernance/montycat-mcp/releases/latest/download/montycat-mcp.mcpb)
and drag it into Claude Desktop. No Python needed. That link always serves the
current release; [every release](https://github.com/MontyGovernance/montycat-mcp/releases/latest)
also carries a version-named copy and a `.sha256` to check it against.

**Claude Code** — install [`uv`](https://docs.astral.sh/uv/getting-started/installation/), then:

```text
/plugin marketplace add MontyGovernance/montycat-mcp
/plugin install montycat-mcp@montygovernance
```

`/mcp` confirms the `montycat` server is connected.

**Codex, Cursor, other MCP clients** — point your client's stdio config at
`uvx montycat-mcp` (Python 3.10+). For Codex:

```bash
codex mcp add montycat -- uvx montycat-mcp
```

## The engine

Memory lives in a [Montycat Semantic](https://montygovernance.com) engine.
Montycat MCP starts a local one for you, so most people can stop reading here.

Point it at an engine you already run:

```bash
export MONTYCAT_URI="montycat://memory-agent:password@localhost:21210/memories"
export MONTYCAT_TLS=true   # remote engines only
```

Or start one yourself with Docker:

```bash
docker run -d --name montycat -p 21210:21210 -p 21211:21211 \
  -e MONTYCAT_SUPEROWNER=admin -e MONTYCAT_PASSWORD=change-me \
  -v montycat_data:/var/lib/.montycat \
  montygovernance/montycat:semantic
```

On Apple Silicon use the `arm64-semantic` tag instead — `semantic` is the amd64
image, and it crashes under emulation. Port `21211` carries live memory watches.

## Use it

Talk to your agent normally; it picks the tool.

> Remember that the team chose PostgreSQL for the billing service.

> What did we decide about the billing database?

> Save this to the shared `engineering` scope.

`scope` decides where a memory lives — `alice` for private, `engineering` for a
team, `shared` for common. It is a namespace, not a security boundary: for real
isolation, give each MCP server its own least-privilege Montycat credential.

### Tools

| Need | Tools |
|---|---|
| Store | `montycat_remember`, `montycat_remember_bulk`, `montycat_update`, `montycat_forget` |
| Recall | `montycat_semantic_search`, `montycat_recall`, `montycat_list_memories` |
| Inspect schemas | `montycat_list_enforced_schemas` — check field names and data types before structured writes or filtered retrieval |
| Collaborate | `montycat_await_memory_change` — wait for another agent's write, no polling |
| Namespaces | `montycat_list_keyspaces`, `montycat_create_keyspace`, `montycat_remove_keyspace` |
| Admin | semantic index, snapshot, and policy tools — see the [plugin guide](plugins/montycat-mcp/README.md) |

Destructive tools are declared as such, so your client's confirmation prompts apply.

## Configuration

| Variable | Purpose |
|---|---|
| `MONTYCAT_URI` | Connection string: `montycat://user:password@host:port/store` |
| `MONTYCAT_TLS` | `true` for a remote TLS engine |
| `MONTYCAT_DEFAULT_KEYSPACE` | Memory namespace; `memory` by default |
| `MONTYCAT_SCOPE` | Default scope when a call omits one |
| `MONTYCAT_AUTO_PROVISION` | Create a permitted scope on first use; `true` by default |
| `MONTYCAT_AUTOSTART` | `off` to require an already-running engine |

Compose setup and the full variable list: [compose.yaml](compose.yaml) and the
[plugin guide](plugins/montycat-mcp/README.md).

## More

[Changelog](CHANGELOG.md) · [Privacy](PRIVACY.md) · [Issues](https://github.com/MontyGovernance/montycat-mcp/issues) · [Docs](https://montygovernance.com/docs) · [Docker Hub](https://hub.docker.com/r/montygovernance/montycat)

Existing MemoCat installs keep working: `memocat-mcp`, `MEMOCAT_*`, and
`memocat://` are still supported. New setups should use the Montycat names.

MIT
