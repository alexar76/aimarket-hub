"""LUMEN trust oracle client — PageRank/EigenTrust over the supply trust graph."""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def clamp01(n: float) -> float:
    if not isinstance(n, (int, float)) or n != n:
        return 0.5
    return max(0.0, min(1.0, float(n)))


class LumenTrustClient:
    """Calls ``lumen.reputation@v1`` on the oracle-family Hub endpoint."""

    def __init__(self, oracle_family_url: str, timeout_s: float = 8.0):
        self._base = oracle_family_url.rstrip("/")
        self._timeout = timeout_s

    def score_entity(self, entity_id: str, edges: list[tuple[str, str, float]]) -> dict[str, Any]:
        """Return {score, degraded, rank?, nodes, edges_count}."""
        if not edges:
            return {"score": 0.5, "degraded": True, "reason": "no_edges"}

        nodes: list[str] = []
        index: dict[str, int] = {}

        def idx(n: str) -> int:
            if n not in index:
                index[n] = len(nodes)
                nodes.append(n)
            return index[n]

        lumen_edges: list[list[float | int]] = []
        for src, dst, w in edges:
            if w == 0:
                continue
            lumen_edges.append([idx(src), idx(dst), float(w)])

        if entity_id not in index:
            idx(entity_id)

        if not lumen_edges:
            return {"score": 0.5, "degraded": True, "reason": "empty_graph"}

        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.post(
                    f"{self._base}/ai-market/v2/invoke",
                    json={
                        "capability_id": "lumen.reputation@v1",
                        "input": {"nodes": len(nodes), "edges": lumen_edges, "damping": 0.85},
                    },
                )
                if resp.status_code != 200:
                    logger.warning("LUMEN HTTP %s", resp.status_code)
                    return {"score": 0.5, "degraded": True, "reason": f"http_{resp.status_code}"}
                payload = resp.json()
                output = payload.get("output") or payload.get("result") or payload
                scores = output.get("scores") if isinstance(output, dict) else None
                if not isinstance(scores, list) or not scores:
                    return {"score": 0.5, "degraded": True, "reason": "bad_output"}
                target = index[entity_id]
                if target >= len(scores):
                    return {"score": 0.5, "degraded": True, "reason": "index_oob"}
                raw = float(scores[target])
                rank = 1 + sum(1 for s in scores if float(s) > raw)
                percentile = sum(1 for s in scores if float(s) <= raw) / len(scores)
                return {
                    "score": clamp01(percentile),
                    "degraded": False,
                    "rank": rank,
                    "nodes": len(nodes),
                    "edges_count": len(lumen_edges),
                    "converged": output.get("converged"),
                }
        except Exception as exc:
            logger.warning("LUMEN unreachable: %s", exc)
            return {"score": 0.5, "degraded": True, "reason": str(exc)[:120]}
