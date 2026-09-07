# syntax=docker/dockerfile:1
# MCP is a stdio server, not an HTTP service. Run this image with `-i` (or via
# `docker compose run -T mcp`) and keep Montycat in the companion service.
FROM python:3.12-slim

ARG VERSION=1.1.1
ARG VCS_REF=unknown

LABEL org.opencontainers.image.title="Montycat MCP" \
      org.opencontainers.image.description="Shared, persistent memory across AI agents and systems through MCP" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.source="https://github.com/MontyGovernance/montycat-mcp" \
      org.opencontainers.image.url="https://github.com/MontyGovernance/montycat-mcp" \
      org.opencontainers.image.documentation="https://github.com/MontyGovernance/montycat-mcp/blob/master/README.md" \
      org.opencontainers.image.licenses="MIT" \
      io.modelcontextprotocol.server.name="io.github.MontyGovernance/montycat-mcp"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MONTYCAT_AUTOSTART=off

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY montycat_mcp ./montycat_mcp
COPY memocat_mcp ./memocat_mcp

RUN pip install --no-cache-dir .

RUN groupadd --system montycat \
    && useradd --system --gid montycat --home-dir /nonexistent --no-create-home montycat

USER montycat

ENTRYPOINT ["montycat-mcp"]
