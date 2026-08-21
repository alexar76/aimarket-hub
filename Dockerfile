# ── HEPHAESTUS studio bundle ──────────────────────────────────
# Built here rather than committed: this repository does not track build output
# (`dist/` is ignored everywhere), and the runtime image is Python-only, so the one
# place a Node toolchain can run for it is a build stage.
FROM node:20-alpine AS studio
WORKDIR /build
COPY hephaestus/package.json ./package.json
COPY hephaestus/src/ ./src/
COPY hephaestus/studio/package.json hephaestus/studio/package-lock.json hephaestus/studio/tsconfig.json hephaestus/studio/vite.config.ts hephaestus/studio/index.html ./studio/
WORKDIR /build/studio
# `ci`, not `install`: a production image should build the versions that were tested,
# and `install` is free to resolve something newer at image-build time.
RUN npm ci --silent
COPY hephaestus/studio/src/ ./src/
RUN npm run build

FROM python:3.12-slim

LABEL org.opencontainers.image.title="modelmarket Hub"
LABEL org.opencontainers.image.description="Federation hub for AI capability discovery and routing"
LABEL org.opencontainers.image.version="3.2.1"
LABEL org.opencontainers.image.licenses="Apache-2.0"

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Build from repository root:
#   docker build -f aimarket-hub/Dockerfile -t modelmarket-hub .

# ── Core hub ──────────────────────────────────────────────────
COPY aimarket-hub/pyproject.toml ./pyproject.toml
COPY aimarket-hub/aimarket_hub/ ./aimarket_hub/
COPY aimarket-hub/plugins-demo.html /app/plugins-demo.html
COPY aimarket-hub/terminal-home.html /app/terminal-home.html
COPY aimarket-hub/cap-descriptions-i18n.json /app/cap-descriptions-i18n.json
COPY aimarket-hub/hub-ui-i18n.json /app/hub-ui-i18n.json
# [escrow] pulls eth-utils/eth-account — without them aimarket_hub.escrow_bridge
# raises CryptoUnavailable and every escrow-funded channel open is refused. That is
# the only funding path this image has: the tx-hash verifier lives in the web stack
# (web.backend.services.ai_market_protocol.on_chain), which is not copied here.
# Refresh the installer itself before it installs anything. pip 24.0/25.0.1 and
# setuptools 79.0.1 shipped in the base image carry known advisories (6 and 1
# respectively as of 2026-07-28) — they never touch a request, but they are what a
# scan of the running container reports, and there is no reason to carry them.
RUN pip install --no-cache-dir --upgrade pip setuptools

RUN pip install --no-cache-dir -e ".[escrow,postgres]" && pip install --no-cache-dir uvicorn

# ── ACEX (capital pricing for Pulse Terminal) ─────────────────
COPY acex/ /app/acex/

# ── Protocol schemas (for JSON Schema validation) ─────────────
COPY aimarket-protocol/schemas/ /app/aimarket-protocol/schemas/

# ── HEPHAESTUS studio (served by hub at /studio) ──────────────
COPY --from=studio /build/studio/dist/ /app/hephaestus/studio/dist/

# ── Widget static files (served by hub at /widget/) ───────────
COPY aimarket-widget/ /app/aimarket-widget/

# ── 14 plugin shims that re-export from hub-core (plugins/ also holds the
#    aimarket-provenance monorepo-entry, whose build fails here — its package-dir
#    points outside the build context — and is harmless: provenance is installed
#    canonically in the next step). ─────
COPY plugins/ /tmp/plugins/
RUN for d in /tmp/plugins/*/; do pip install --no-cache-dir "$d" 2>/dev/null || true; done \
    && rm -rf /tmp/plugins

# ── In-hub canonical plugin (provenance) ──────────────────────
# Per docs/repository-canonical-policy.md, `aimarket-hub/plugins/aimarket-provenance/`
# is the canonical implementation (not a shim like the 14 above). It registers via
# the `aimarket.plugins` entry-point and hub-core (api.py) expects its receipts.
# Without this install, `_provenance_receipt` is always None and the
# `provenance_receipts` table stays empty in production.
# The plugin issues AWR/2 documents and implements none of the format itself, so it
# depends on `awr` (RFC 8785 canonicalization, the eddsa-jcs-2022 proof, did:key).
# That package is not on PyPI — it lives in this repo — so it must be installed from
# the build context BEFORE the plugin, or pip resolves `awr>=2.0,<3` against PyPI,
# finds nothing, and the whole image fails to build:
#   ERROR: No matching distribution found for awr<3,>=2.0
COPY awr/reference/python/ /tmp/awr/
RUN pip install --no-cache-dir /tmp/awr && rm -rf /tmp/awr

COPY aimarket-hub/plugins/aimarket-provenance/ /tmp/provenance/
RUN pip install --no-cache-dir /tmp/provenance && rm -rf /tmp/provenance

# ── Data dir ──────────────────────────────────────────────────
RUN mkdir -p /app/data

# ── Non-root runtime user ─────────────────────────────────────
RUN groupadd --system --gid 10001 hub \
    && useradd --system --uid 10001 --gid hub --shell /usr/sbin/nologin --home-dir /app hub \
    && chown -R hub:hub /app

USER hub:hub

ENV AIMARKET_DB_PATH=/app/data/hub.db
ENV AIMARKET_SIGNING_KEY_PATH=/app/data/hub_signing_key
# Default LISTEN address. Set AIMARKET_HUB_URL to your public URL in production
# (e.g. https://hub.example.com) or receipts/manifests will contain broken URLs.
ENV AIMARKET_HUB_URL=http://localhost:9083
ENV AIMARKET_CRAWL_INTERVAL_S=3600

EXPOSE 9083

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:9083/.well-known/ai-market.json || exit 1

ENTRYPOINT ["python", "-m", "aimarket_hub", "serve"]
