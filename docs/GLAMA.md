# Glama build — admin form

Listing: [glama.ai/mcp/servers/alexar76/aimarket-hub](https://glama.ai/mcp/servers/alexar76/aimarket-hub)

Admin: […/admin/dockerfile](https://glama.ai/mcp/servers/alexar76/aimarket-hub/admin/dockerfile)

Glama generates the image from this form. It does not build [`Dockerfile.standalone`](../Dockerfile.standalone). That file is the satellite image: `python:3.12-slim` (Debian Bookworm) and `python -m aimarket_hub serve --init`. CI and the published image use Python 3.12. The only Node pin in this repo is the studio build stage, `node:20-alpine`, which the Glama image does not compile.

The form only offers `debian:bookworm-slim` and `debian:trixie-slim`.

| Field | Value |
| --- | --- |
| Base image | `debian:bookworm-slim` |
| Node.js version | `20` |
| Python version | `3.12` |
| Build steps | `["uv sync --frozen --no-dev --extra escrow --extra pqc"]` |
| CMD arguments | `[".venv/bin/python", "-m", "aimarket_hub", "serve", "--init"]` |
| Environment variables | `{"type":"object","properties":{},"required":[]}` |
| Pinned commit SHA | empty |

Leave every key empty. The hub creates its own dev signing key on `--init`. Production settings are not part of this form.

The process is the HTTP hub. MCP on this service is the HTTP route, not a stdio server, so Glama's `mcp-proxy` probe is not a substitute for `GET /.well-known/ai-market.json`.
