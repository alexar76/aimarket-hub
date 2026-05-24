FROM python:3.12-slim

LABEL org.opencontainers.image.title="modelmarket Hub"
LABEL org.opencontainers.image.description="Federation hub for AI capability discovery and routing"
LABEL org.opencontainers.image.version="3.0.0"
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
RUN pip install --no-cache-dir -e . && pip install --no-cache-dir uvicorn

# ── Protocol schemas (for JSON Schema validation) ─────────────
COPY aimarket-protocol/schemas/ /app/aimarket-protocol/schemas/

# ── Widget static files (served by hub at /widget/) ───────────
COPY aimarket-widget/ /app/aimarket-widget/

# ── All 14 plugins (top-level shims that re-export from hub-core) ─────
COPY plugins/ /tmp/plugins/
RUN for d in /tmp/plugins/*/; do pip install --no-cache-dir "$d" 2>/dev/null || true; done \
    && rm -rf /tmp/plugins

# ── In-hub canonical plugin (provenance) ──────────────────────
# Per docs/repository-canonical-policy.md, `aimarket-hub/plugins/aimarket-provenance/`
# is the canonical implementation (not a shim like the 14 above). It registers via
# the `aimarket.plugins` entry-point and hub-core (api.py) expects its receipts.
# Without this install, `_provenance_receipt` is always None and the
# `provenance_receipts` table stays empty in production.
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
