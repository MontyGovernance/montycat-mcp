# Montycat MCP Changelog

All notable changes to Montycat MCP are documented here.

## 1.1.1 — 2026-09-06

Adds read-only enforced-schema discovery for safer structured memory access.

### Added

- Add the read-only `montycat_list_enforced_schemas` tool to inspect schema
  names, required fields, and data types before writing structured records.
- Guide agents to inspect schemas when a structured write or filtered retrieval
  must conform to uncertain field names or data types.

### Changed

- Advertise `Montycat MCP` as the human-facing server name during the MCP
  handshake while retaining existing package IDs and Claude configuration keys.

## 1.1.0 — 2026-09-03

Aligns with Montycat Semantic 1.3.4 and the Montycat Python client 1.2.3.

### Added

- Add `mode` to `montycat_semantic_search`: `semantic` (default, vector
  similarity), `keyword` (BM25), and `hybrid` (both, fused with reciprocal rank
  fusion). Keyword and hybrid need a Montycat Semantic engine >= 1.3.4; older
  engines reject those modes rather than silently ranking by vector alone.
- Validate `min_score` on the scale of the requested mode — `[-1, 1]` cosine,
  `[0, 1]` normalized hybrid RRF, and `>= 0` for unbounded BM25 — instead of
  applying the cosine range to every mode.

### Changed

- Call the client's unified `search_values` instead of the now-deprecated
  `semantic_search_get_values` / `semantic_search_get_values_where`, and require
  `montycat>=1.2.3`, which introduced it.
- Reserve "hybrid" in tool documentation for the new ranking mode. Metadata,
  `since`, and `until` are described as narrowing the candidate set, which is
  what they have always done.
- Order `montycat_list_memories` by key on persistent keyspaces (engine >=
  1.3.1) instead of reading the latest storage volume. `recent=True` now returns
  strict write order rather than an approximation, and `recent=False` reads from
  the oldest record forward. In-memory keyspaces have no ordered read, so they
  keep the volume heuristic and its empty-volume fallback.

## 1.0.0 — 2026-09-01

### Changed

- Make `montycat_mcp` the canonical Python package and expose the 23 MCP tools
  under `montycat_*` names.
- Change the canonical memory resource scheme to `montycat://` and the Claude
  Desktop extension identity to `io.github.montygovernance.montycat-mcp`.
- Rename MCP-specific configuration to `MONTYCAT_*`, the managed local engine
  container/user/store defaults to Montycat, and all primary examples and
  manifests to version `1.0.0`.
- Mark non-destructive write tools as mutating in MCP metadata instead of
  advertising them as read-only; MCP clients retain control of approval policy.

### Compatibility

- Keep the `memocat-mcp` console command and PyPI package as launchers for the
  current Montycat MCP implementation.
- Keep `memocat_mcp` as a thin import alias, including its `server`, `bootstrap`,
  and `watch` modules and deprecated Python-call aliases.
- Accept legacy `MEMOCAT_*` environment variables when the corresponding
  canonical variable is absent, and continue parsing `memocat://` subscription
  URIs and previously generated `memocat.json` credentials.
- Existing MCP clients that pin tool names must change `memocat_*` calls to
  `montycat_*`; the server advertises one clean 23-tool catalog rather than
  duplicating every tool under both prefixes.

## 0.5.1 — 2026-09-01

### Changed

- Stop requesting user confirmation for routine non-deleting memory writes and
  configuration changes.
- Continue requiring explicit confirmation for delete, rebuild,
  vector-dropping, and snapshot-cleanup operations.
- Keep engine installation approval-required because it downloads software and
  opens the operating system installer.

## 0.5.0 — 2026-09-01

### Added

- Rename the product and primary distribution from MemoCat MCP to Montycat MCP.
  The new package, command, repository, container, Claude plugin, and MCP
  Registry identity use `montycat-mcp`.
- Keep the `memocat-mcp` command, Python import path, `memocat_*` tool names,
  existing environment variables, and Claude Desktop extension identity for
  backward compatibility.
- Add GitHub Releases as the primary Claude Desktop MCPB download path.
- Add automated MCPB validation, packaging, SHA-256 generation, and attachment
  to published GitHub releases.
- Add pull-request CI across Python 3.10–3.13, Python distribution checks,
  isolated wheel and MCPB validation, optional live-engine acceptance tests,
  token-authenticated PyPI publishing, multi-architecture Docker publishing,
  and ordered MCP Registry publication.

### Changed

- Clarify the separate installation paths for Claude Desktop, Claude Code and
  Cowork, and other MCP clients.
- Improve public documentation and plugin metadata for AI memory, MCP server,
  semantic search, RAG, and multi-agent discovery.

### Fixed

- Exclude the private MCPB submission packet and build lockfile from the Claude
  Desktop extension archive.

## 0.4.3 — 2026-08-28

### Added

- Add an MCPB v0.4 Claude Desktop extension using the cross-platform UV
  runtime, generated settings UI, privacy policy, and reproducible packaging.
- Add human-readable titles and explicit read-only, mutating, and destructive
  MCP annotations for all 23 tools.
- Add release gates that synchronize package/manifest versions and require a
  512×512 transparent directory icon.
- Add `memocat_install_engine`, which downloads the Montycat package and opens
  the operating system's installer. Engine installation is no longer something
  that can happen without being asked for.
- Verify a local installation with `montycat version` before launch. MemoCat
  now detects incompatible architectures, missing runtime libraries, and
  execution-permission problems immediately, and distinguishes the base and
  Semantic editions. `MEMOCAT_ENGINE_CLI` can override the CLI location.

### Changed

- Position MemoCat consistently as vendor-neutral shared AI memory across
  Claude, OpenAI Codex, Cursor, and other MCP-compatible systems; the MCPB is
  the Claude Desktop installation surface, not the boundary of the product.
- Serve MCP immediately while acquiring the engine in the background. Tools
  now report engine startup progress instead of delaying the MCP handshake.
  `MEMOCAT_READY_TIMEOUT` (default 20 seconds) bounds each tool's wait.
- Do not start a local engine when `MONTYCAT_HOST` identifies a remote machine.

### Fixed

- Send an identifying `User-Agent` with release-catalog and artifact requests,
  restoring installer discovery and downloads on hosts that reject urllib's
  default user agent.
- Restore saved credentials when reconnecting to a previously managed local
  Montycat engine, so a new Claude Desktop session can use the existing engine.
- Fall back correctly when a generated extension setting arrives empty rather
  than absent, so a cleared keyspace or startup-mode field uses its default.
- Stop advising `MEMOCAT_AUTOSTART=off` in the error raised *because* autostart
  is off.

## 0.4.2 — 2026-08-27

### Added

- Add the official MCP Registry ownership marker and `server.json` manifest for
  `io.github.MontyGovernance/memocat-mcp`.
- Prepare a multi-architecture Docker Hub distribution with OCI metadata and
  the official MCP Registry ownership label.
- Run the MCP image as an unprivileged user and wait for the Compose Semantic
  engine to become healthy before starting MCP.

## 0.4.1 — 2026-08-27

### Fixed

- Pass `semantic=False` through keyspace creation instead of accidentally
  inheriting the Python client's `semantic=True` default.

## 0.4.0

First public release.

### Added

- Nineteen MCP tools for semantic memory, exact recall, bulk operations,
  keyspace lifecycle, governance, semantic management, snapshots, and live
  memory-change watches.
- Hybrid semantic retrieval with metadata and indexed time-range filtering.
- Automatic `_created_at` timestamps for temporal agent memory.
- Persistent, in-memory, private-scope, and shared-scope memory routing.
- MCP resources with live `resources/updated` notifications.
- Delegated-owner policy view, explanation, history, provisioning, removal,
  semantic management, and snapshot management.
- Revocation-aware authorization leases that close watches and purge buffered
  data after access loss.
- Native engine discovery and verified platform-package bootstrap, with Docker
  fallback.
- Precomputed-vector memory writes and queries, external-vector profile
  enrollment, semantic status inspection, and semantic re-embedding.

### Security

- Engine-enforced cross-owner isolation and a 24-scenario live governance
  acceptance matrix.
- Safe keyspace removal that releases subscriptions before engine teardown.
- Mandatory SHA-256 verification for downloaded engine packages.
