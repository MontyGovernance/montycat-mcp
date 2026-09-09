# `memocat-mcp` compatibility package

MemoCat MCP was renamed to [Montycat
MCP](https://github.com/MontyGovernance/montycat-mcp). This package preserves
the former `memocat-mcp` installation and command name while installing the
current `montycat-mcp` implementation.

Version 1.0 advertises canonical `montycat_*` MCP tool names. Clients that pin
the former `memocat_*` names must update those calls.

New installations should use:

```bash
uvx montycat-mcp 
```
