"""Supply-side security — stake, anti-spam, LUMEN trust, response signatures, input limits."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from aimarket_hub.channels import _is_production_mode
from aimarket_hub.config import HubConfig
from aimarket_hub.database import HubDatabase
from aimarket_hub.lumen_client import LumenTrustClient
from aimarket_hub.models import Capability
from aimarket_hub.signing import Signer

logger = logging.getLogger(__name__)

_HUB_ANCHOR = "hub:local"
_SENSITIVE_KEY = re.compile(
    r"(password|secret|api[_-]?key|private[_-]?key|mnemonic|seed|token|ssn|credit)",
    re.I,
)


@dataclass
class SupplySecurityPolicy:
    min_stake_usd: float = 10.0
    publish_per_hour: int = 5
    min_trust_discover: float = 0.25
    min_trust_invoke: float = 0.35
    require_response_signature: bool = True
    max_input_keys: int = 32
    max_input_json_bytes: int = 32_768
    product_allowlist: tuple[str, ...] = ()
    relaxed: bool = False
    # Griefing-resistant provider slashing: only slash after this many genuine
    # failures within the window, not on every single failure.
    slash_failure_threshold: int = 3
    slash_failure_window_s: float = 600.0
    # Calibration: slash is a trust floor, not a death penalty. After a slash
    # fires, further failure-driven slashes are suppressed for the cool-down
    # (failures keep being recorded and keep hurting trust), and the rolling
    # 24h total of failure-driven slashes is capped — one bad day can't zero a
    # new agent's stake. 0 disables the respective knob.
    slash_cooldown_s: float = 3600.0
    slash_daily_cap_usd: float = 10.0
    # Verify-first escalation: a Metis verdict "failed" already refunded the
    # buyer (escrow), so a single verified failure costs trust, not stake.
    # Only REPEAT verified failures within the window escalate to a slash — and
    # only when they come from >= verified_fail_min_consumers DISTINCT paying
    # consumers. The Metis verdict is judged against a buyer-supplied intent the
    # buyer fully controls, so (unlike a provider-side 5xx) one actor's repeated
    # failures are one voice, never proof of misbehavior. Mirrors the weak-tier
    # federation rule (a lone issuer moves nothing).
    verified_fail_threshold: int = 3
    verified_fail_window_s: float = 86_400.0
    verified_fail_min_consumers: int = 2

    @staticmethod
    def _positive_window(env_name: str, default: float) -> float:
        """A slash WINDOW must stay strictly positive. A non-positive (or empty/garbage)
        value would build an invalid ``datetime('now', '--N seconds')`` modifier → SQLite
        returns NULL → the fault count is always 0 → the slash ladder silently fail-OPENS.
        Fall back to the documented default with a warning instead (fail-closed, visible).
        Unlike the cool-down/cap knobs, 0 is NOT a documented 'disable' value for windows."""
        raw = os.environ.get(env_name, str(default))
        try:
            val = float(raw)
        except ValueError:
            val = -1.0
        if val <= 0:
            logger.warning(
                "%s=%r is non-positive; falling back to %s (a non-positive window would "
                "silently disable slashing)", env_name, raw, default,
            )
            return float(default)
        return val

    @classmethod
    def from_config(cls, config: HubConfig) -> SupplySecurityPolicy:
        relaxed = os.environ.get("AIMARKET_SUPPLY_SECURITY_RELAXED", "").strip() == "1"
        allow_raw = os.environ.get("AIMARKET_SUPPLY_PRODUCT_ALLOWLIST", "").strip()
        allowlist = tuple(x.strip() for x in allow_raw.split(",") if x.strip())
        prod = _is_production_mode()
        default_stake = "25" if prod and not relaxed else "10"
        min_stake = float(os.environ.get("AIMARKET_SUPPLY_MIN_STAKE_USD", default_stake))
        if relaxed:
            min_stake = 0.0
        require_sig = os.environ.get("AIMARKET_SUPPLY_REQUIRE_RESPONSE_SIG", "").strip()
        if require_sig == "":
            require_sig_bool = _is_production_mode() and not relaxed
        else:
            require_sig_bool = require_sig not in ("0", "false", "no")
        return cls(
            min_stake_usd=min_stake,
            publish_per_hour=int(os.environ.get("AIMARKET_SUPPLY_PUBLISH_PER_HOUR", "5")),
            min_trust_discover=float(
                os.environ.get("AIMARKET_SUPPLY_MIN_TRUST_DISCOVER", str(config.min_trust_score))
            ),
            min_trust_invoke=float(os.environ.get("AIMARKET_SUPPLY_MIN_TRUST_INVOKE", "0.35")),
            require_response_signature=require_sig_bool,
            max_input_keys=int(os.environ.get("AIMARKET_SUPPLY_MAX_INPUT_KEYS", "32")),
            max_input_json_bytes=int(os.environ.get("AIMARKET_SUPPLY_MAX_INPUT_JSON_BYTES", "32768")),
            product_allowlist=allowlist,
            relaxed=relaxed,
            slash_failure_threshold=int(os.environ.get("AIMARKET_SUPPLY_SLASH_FAILURE_THRESHOLD", "3")),
            slash_failure_window_s=cls._positive_window("AIMARKET_SUPPLY_SLASH_FAILURE_WINDOW_S", 600),
            slash_cooldown_s=float(os.environ.get("AIMARKET_SUPPLY_SLASH_COOLDOWN_S", "3600")),
            slash_daily_cap_usd=float(os.environ.get("AIMARKET_SUPPLY_SLASH_DAILY_CAP_USD", "10")),
            verified_fail_threshold=int(os.environ.get("AIMARKET_SUPPLY_VERIFIED_FAIL_THRESHOLD", "3")),
            verified_fail_window_s=cls._positive_window("AIMARKET_SUPPLY_VERIFIED_FAIL_WINDOW_S", 86400),
            verified_fail_min_consumers=int(os.environ.get("AIMARKET_SUPPLY_VERIFIED_FAIL_MIN_CONSUMERS", "2")),
        )


class SupplySecurity:
    """Guards publish + invoke for community ``invoke_url`` capabilities."""

    def __init__(
        self,
        db: HubDatabase,
        config: HubConfig,
        signer: Signer | None = None,
        slash_registry: Any = None,
    ):
        self.db = db
        self.config = config
        self.signer = signer or Signer()
        self.slash_registry = slash_registry
        self.policy = SupplySecurityPolicy.from_config(config)
        oracle_url = os.environ.get(
            "AIMARKET_ORACLE_FAMILY_URL",
            os.environ.get("ARGUS_ORACLE_FAMILY_URL", "https://oracles.modelmarket.dev/family"),
        )
        self.lumen = LumenTrustClient(oracle_url)

    # ── Publish guards ────────────────────────────────────────

    def validate_publish(self, manifest: dict[str, Any]) -> tuple[str, str]:
        """Returns (publisher_id, provider_pubkey). Raises ValueError."""
        publisher_id = str(manifest.get("publisher_id") or manifest.get("publisher") or "").strip()
        if not publisher_id:
            raise ValueError("publisher_id is required (wallet address or stable publisher slug)")
        product_id = str(manifest.get("product_id", "")).strip()
        if self.policy.product_allowlist and product_id not in self.policy.product_allowlist:
            raise ValueError(f"product_id not on allowlist: {product_id}")
        pubkey = str(manifest.get("provider_pubkey", "")).strip()
        if self.policy.require_response_signature and not pubkey:
            raise ValueError("provider_pubkey is required — responses must be Ed25519-signed")
        invoke_url = str(manifest.get("invoke_url", "")).strip()
        existing = self.db.supply_capability_by_invoke_url(invoke_url)
        if existing and existing.product_id != product_id:
            raise ValueError("invoke_url already registered to another product (dedup)")
        if self.policy.publish_per_hour > 0:
            recent = self.db.supply_publish_count_recent(publisher_id, hours=1)
            if recent >= self.policy.publish_per_hour:
                raise ValueError(f"publish rate limit exceeded ({self.policy.publish_per_hour}/hour)")
        if self.policy.min_stake_usd > 0:
            stake = self.db.supply_stake_get(publisher_id)
            if stake < self.policy.min_stake_usd:
                raise ValueError(
                    f"minimum stake ${self.policy.min_stake_usd:.2f} required "
                    f"(current ${stake:.2f}) — POST /ai-market/v2/supply/stake"
                )
        return publisher_id, pubkey

    def after_publish(self, cap: Capability, publisher_id: str) -> float:
        self.db.supply_publish_log(publisher_id, cap.product_id, cap.invoke_url or "")
        trust = self.refresh_publisher_trust(publisher_id)
        cap.trust_score = trust
        cap.publisher_id = publisher_id
        self.db.upsert_capability(cap)
        return trust

    # ── Invoke guards ─────────────────────────────────────────

    def sanitize_input(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("input must be a JSON object")
        raw = json.dumps(payload, ensure_ascii=False)
        if len(raw.encode("utf-8")) > self.policy.max_input_json_bytes:
            raise ValueError(f"input exceeds {self.policy.max_input_json_bytes} bytes")
        if len(payload) > self.policy.max_input_keys:
            raise ValueError(f"input has more than {self.policy.max_input_keys} top-level keys")
        for key in payload:
            if _SENSITIVE_KEY.search(str(key)):
                raise ValueError(f"sensitive field name blocked in input: {key}")
        return payload

    def check_invoke_trust(self, cap: Capability) -> None:
        if not (cap.invoke_url or "").strip():
            return
        min_t = self.policy.min_trust_invoke
        if cap.trust_score < min_t:
            raise ValueError(
                f"capability trust {cap.trust_score:.3f} below minimum {min_t:.3f} for invoke"
            )

    def verify_provider_response(
        self,
        cap: Capability,
        result: Any,
        signature_b64: str,
        *,
        product_id: str = "",
        input_payload: Any = None,
    ) -> None:
        if not (cap.invoke_url or "").strip():
            return
        if not self.policy.require_response_signature:
            return
        pubkey = (cap.provider_pubkey or "").strip()
        if not pubkey:
            raise ValueError("capability missing provider_pubkey")
        if not signature_b64:
            raise ValueError("provider response signature required (X-Provider-Signature)")
        # Preferred: signature bound to the REQUEST (capability + product + input hash),
        # so a provider can't replay a previously-signed response for a different
        # request. Fall back to the legacy result-only canonical for providers that
        # have not yet upgraded, with a deprecation warning — this closes replay for
        # updated providers without breaking existing federation signatures.
        bound = _bound_response_canonical(cap.capability_id, product_id, input_payload, result)
        if self.signer.verify(pubkey, signature_b64, bound):
            return
        if self.signer.verify(pubkey, signature_b64, _response_canonical(result)):
            logger.warning(
                "provider %s signed a legacy result-only response (replayable); "
                "provider should upgrade to the request-bound signature",
                cap.capability_id,
            )
            return
        raise ValueError("invalid provider response signature")

    def record_invoke(
        self,
        *,
        publisher_id: str,
        consumer_id: str,
        success: bool,
        product_id: str,
        capability_id: str,
    ) -> float:
        weight = 0.15 if success else -0.25
        event = "invoke_success" if success else "invoke_failure"
        self.db.trust_add_edge(consumer_id or "consumer:anonymous", publisher_id, weight, event)
        if success:
            # A success clears the consecutive-failure streak for this provider.
            self.db.supply_fault_clear(publisher_id, "invoke_failure")
        else:
            # Griefing-resistant slashing: slash only after N genuine failures within
            # the window, not on every single failure — a lone failure (or a burst of
            # them driven by one consumer) must not drain provider stake. Transient
            # provider-unreachability and consumer-side 4xx never reach here (see the
            # /invoke handler); only genuine provider faults (5xx / bad response
            # signature) are recorded as failures. The streak is DB-backed so a
            # restart doesn't amnesty it.
            self.db.supply_fault_log(publisher_id, "invoke_failure", product_id, capability_id)
            fails = self.db.supply_fault_count_recent(
                publisher_id, "invoke_failure", self.policy.slash_failure_window_s
            )
            if fails >= self.policy.slash_failure_threshold:
                self._slash_for_failure(publisher_id, product_id, capability_id, reason="invoke_failure")
                # Reset after acting so we don't slash again on every later failure.
                self.db.supply_fault_clear(publisher_id, "invoke_failure")
        return self.refresh_publisher_trust(publisher_id)

    def record_verified_failure(
        self,
        *,
        publisher_id: str,
        product_id: str,
        capability_id: str,
        consumer_id: str = "",
        paid: bool = False,
        rejection: dict[str, Any] | None = None,
    ) -> None:
        """Verify-first escalation: a genuine Metis verdict "failed" against this provider.

        Griefing-resistant by construction — the Metis verdict is judged against a
        buyer-supplied ``intent`` the buyer fully controls (unlike a provider-side
        5xx, which the provider controls), so a "failed" verdict is only weak evidence:

        * **Only a PAID verification** (real escrow-backed transaction) can feed the
          stake ladder. An advisory/free verdict (sandbox, crypto-off, sub-floor) risked
          nothing for the buyer, so it must never cost the provider stake — it is a
          no-op here (the soft ``verify_failed`` reputation signal still fires upstream
          in ``_emit_reputation`` regardless).
        * The trust ding is attributed to the **consumer** node (not the hub anchor),
          so a low-rank/anonymous buyer's complaint carries little structural weight and
          cannot be hub-amplified.
        * A slash requires BOTH the failure-count threshold AND **≥ N distinct paying
          consumers** (``verified_fail_min_consumers``). One buyer's repeated failures
          count as a single voice and can never drain a competitor's stake alone —
          mirroring the weak-tier federation rule (a lone issuer moves nothing).

        The buyer is already made whole by the escrow refund; slash is the last resort
        for a provider that repeatedly fails DISTINCT paying buyers, not the QA system.
        """
        publisher_id = (publisher_id or "").strip()
        if not publisher_id:
            return
        # Advisory (unpaid) verdicts never reach the stake ladder — see docstring.
        if not paid:
            return
        consumer = (consumer_id or "").strip() or "consumer:anonymous"
        self.db.trust_add_edge(consumer, publisher_id, -0.25, "verified_failure")
        self.db.supply_fault_log(publisher_id, "verified_failure", product_id, capability_id, consumer_id=consumer)
        fails = self.db.supply_fault_count_recent(
            publisher_id, "verified_failure", self.policy.verified_fail_window_s
        )
        distinct = self.db.supply_fault_distinct_consumers_recent(
            publisher_id, "verified_failure", self.policy.verified_fail_window_s
        )
        if fails >= self.policy.verified_fail_threshold and distinct >= self.policy.verified_fail_min_consumers:
            self._slash_for_failure(
                publisher_id, product_id, capability_id,
                reason="verified_failure",
                evidence=rejection,
                evidence_kind="verification_rejection" if rejection else "",
            )
            self.db.supply_fault_clear(publisher_id, "verified_failure")
        self.refresh_publisher_trust(publisher_id)

    def refresh_publisher_trust(self, publisher_id: str) -> float:
        edges_raw = self.db.trust_list_edges(limit=1000)
        edges: list[tuple[str, str, float]] = []
        stake = self.db.supply_stake_get(publisher_id)
        if stake > 0:
            import math
            w = min(math.log10(max(stake, 1)) / 2.0, 1.0)
            edges.append((_HUB_ANCHOR, publisher_id, w))
        for src, dst, w, _ in edges_raw:
            edges.append((src, dst, w))
        result = self.lumen.score_entity(publisher_id, edges)
        score = float(result.get("score", 0.5))
        self.db.supply_set_publisher_trust(publisher_id, score)
        return score

    def filter_for_discover(self, caps: list[Capability]) -> list[Capability]:
        min_t = self.policy.min_trust_discover
        seen_urls: set[str] = set()
        out: list[Capability] = []
        for c in sorted(caps, key=lambda x: x.trust_score, reverse=True):
            if (c.invoke_url or "").strip():
                if c.trust_score < min_t:
                    continue
                url = c.invoke_url.strip()
                if url in seen_urls:
                    continue
                seen_urls.add(url)
            out.append(c)
        return out

    # ── Stake / slash ─────────────────────────────────────────

    def stake(self, publisher_id: str, amount_usd: float, tx_hash: str = "") -> dict[str, Any]:
        if amount_usd <= 0:
            raise ValueError("amount_usd must be positive")
        tx_hash = (tx_hash or "").strip()
        # In production a real stake must be backed by a verified, single-use
        # on-chain deposit. Previously the tx_hash was merely *recorded* — never
        # verified and never deduplicated — so a publisher could spoof stake
        # (and thus trust score / stake-gate) with a fabricated or reused hash.
        if not self.policy.relaxed and _is_production_mode() and amount_usd >= self.policy.min_stake_usd:
            if not tx_hash:
                raise ValueError(
                    "tx_hash required for stake deposits in production "
                    "(AIMARKET_SUPPLY_SECURITY_RELAXED=1 to bypass for dev)"
                )
            if self.db.supply_stake_tx_exists(tx_hash):
                raise ValueError("stake tx_hash already recorded (replay rejected)")
            if not _verify_stake_tx(tx_hash, amount_usd):
                raise ValueError(
                    "stake deposit not verified on-chain — the transaction must pay the "
                    "platform recipient at least the staked amount with enough confirmations"
                )
        total = self.db.supply_stake_add(publisher_id, amount_usd, tx_hash)
        import math
        self.db.trust_add_edge(_HUB_ANCHOR, publisher_id, min(math.log10(max(total, 1)) / 2.0, 1.0), "stake")
        trust = self.refresh_publisher_trust(publisher_id)
        return {"publisher_id": publisher_id, "stake_usd": total, "trust_score": trust}

    def slash(
        self,
        publisher_id: str,
        amount_usd: float,
        reason: str,
        *,
        evidence: dict[str, Any] | None = None,
        evidence_kind: str = "",
    ) -> dict[str, Any]:
        slashed, remaining = self.db.supply_stake_slash(publisher_id, amount_usd)
        self.db.trust_add_edge(_HUB_ANCHOR, publisher_id, -0.5, "slash")
        trust = self.refresh_publisher_trust(publisher_id)
        if slashed > 0:
            # Durable per-event log: feeds the cool-down + rolling daily cap and
            # keeps every slash auditable (the stake row only holds a cumulative sum).
            self.db.supply_slash_log(publisher_id, slashed, reason, evidence_kind)
        federated = False
        if self.slash_registry and slashed > 0:
            try:
                self.slash_registry.record_local_slash(
                    provider_hub=publisher_id,
                    slashed_usd=slashed,
                    dispute_id=f"supply_{int(time.time())}",
                    reason=reason,
                    signer=self.signer,
                    pom=None,
                    evidence=evidence,
                    evidence_kind=evidence_kind,
                )
                federated = True
            except Exception:
                federated = False
        return {
            "publisher_id": publisher_id,
            "slashed_usd": slashed,
            "stake_remaining_usd": remaining,
            "trust_score": trust,
            "federated": federated,
            "reason": reason,
        }

    def _slash_for_failure(
        self,
        publisher_id: str,
        product_id: str,
        capability_id: str,
        reason: str,
        *,
        evidence: dict[str, Any] | None = None,
        evidence_kind: str = "",
    ) -> None:
        if self.policy.relaxed:
            return
        stake = self.db.supply_stake_get(publisher_id)
        if stake <= 0:
            return
        # Cool-down: at most one failure-driven slash per window. Failures during
        # the cool-down still count against trust — only the stake is spared.
        cooldown = self.policy.slash_cooldown_s
        if cooldown > 0:
            age = self.db.supply_slash_last_age_s(publisher_id)
            if age is not None and age < cooldown:
                logger.info(
                    "slash cool-down active for %s (%.0fs of %.0fs) — %s not slashed",
                    publisher_id, age, cooldown, reason,
                )
                return
        slash_amt = min(stake * 0.05, 5.0)
        # Rolling daily cap: repeated bad days drain reputation and discoverability,
        # not the whole bond at once (innovation room for agents that can recover).
        cap = self.policy.slash_daily_cap_usd
        if cap > 0:
            already = self.db.supply_slash_total_recent(publisher_id, hours=24.0)
            slash_amt = min(slash_amt, max(0.0, cap - already))
            if slash_amt <= 0:
                logger.info(
                    "daily slash cap $%.2f reached for %s — %s not slashed",
                    cap, publisher_id, reason,
                )
                return
        self.slash(
            publisher_id, slash_amt, f"{reason}:{product_id}/{capability_id}",
            evidence=evidence, evidence_kind=evidence_kind,
        )

    # ── Self-bond (consumer cost/conduct bond → self-slash enforcement) ──
    # ARGUS stakes collateral and registers a bonded spend CEILING + its client self-bond
    # commitment. /self-bond/slash slashes that staked collateral on a declared-ceiling-
    # vs-observed-spend breach and federates the attestation. HONEST SCOPE: the hub slashes
    # against the observed spend SUBMITTED with the dispute (mirroring ProofOfMisbehavior);
    # cross-checking it against hub-issued settlement receipts is the deeper follow-up.

    def register_self_bond(
        self,
        agent_id: str,
        evm_address: str,
        ceiling_usd: float,
        bond_usd: float,
        commitment: str = "",
    ) -> dict[str, Any]:
        agent_id = str(agent_id or "").strip()
        if not agent_id:
            raise ValueError("agent_id required")
        if bond_usd <= 0 or ceiling_usd < 0:
            raise ValueError("bond_usd must be positive and ceiling_usd non-negative")
        # The bond must be backed by real staked collateral (same store publishers use).
        staked = self.db.supply_stake_get(agent_id)
        if not self.policy.relaxed and staked < bond_usd:
            raise ValueError(
                f"bond ${bond_usd:.2f} exceeds staked collateral ${staked:.2f} — "
                f"POST /ai-market/v2/supply/stake (publisher_id={agent_id}) first"
            )
        self.db.self_bond_register(agent_id, str(evm_address or ""), float(ceiling_usd), float(bond_usd), "USDC", str(commitment or ""))
        return {
            "agent_id": agent_id,
            "ceiling_usd": float(ceiling_usd),
            "bond_usd": float(bond_usd),
            "staked_usd": staked,
            "commitment": str(commitment or ""),
            "status": "bonded",
        }

    def slash_self_bond(self, agent_id: str, observed_spend_usd: float, evidence: str = "") -> dict[str, Any]:
        agent_id = str(agent_id or "").strip()
        bond = self.db.self_bond_get(agent_id)
        if not bond:
            raise ValueError(f"no self-bond registered for agent_id={agent_id}")
        ceiling = float(bond.get("ceiling_usd", 0))
        bond_usd = float(bond.get("bond_usd", 0))
        observed = max(0.0, float(observed_spend_usd))
        overspend = max(0.0, observed - ceiling)
        if overspend <= 0:
            return {
                "agent_id": agent_id, "verdict": "within-bond", "overspend_usd": 0.0,
                "slashed_usd": 0.0, "ceiling_usd": ceiling, "observed_spend_usd": observed,
            }
        penalty = min(bond_usd, overspend)  # honest: capped at the declared bond
        result = self.slash(agent_id, penalty, f"self_bond_breach:declared_ceiling_{ceiling:.6f}_lt_observed_{observed:.6f}")
        self.db.self_bond_record_slash(agent_id, float(result.get("slashed_usd", 0.0)))
        return {
            "agent_id": agent_id, "verdict": "self-slash",
            "overspend_usd": overspend, "ceiling_usd": ceiling, "observed_spend_usd": observed,
            "slashed_usd": result.get("slashed_usd", 0.0),
            "stake_remaining_usd": result.get("stake_remaining_usd", 0.0),
            "federated": result.get("federated", False),
            "evidence": str(evidence or "")[:200],
        }


def _verify_stake_tx(tx_hash: str, amount_usd: float) -> bool:
    """Verify a stake deposit paid the platform recipient >= amount_usd on-chain.

    Uses the same fail-closed verifier as payment channels. Returns False (→ the
    caller rejects) when the verifier is unreachable, so a standalone/misconfigured
    deploy never accepts an unverified stake in production.
    """
    try:
        from web.backend.services.ai_market_protocol.on_chain import verify_tx_payment
    except Exception:
        logger.error("stake tx verification unavailable (verifier import failed) — rejecting")
        return False
    chain = os.getenv("AIMARKET_PAYMENT_CHAIN", "base")
    token = os.getenv("AIMARKET_PAYMENT_TOKEN", "USDC")
    try:
        return bool(verify_tx_payment(tx_hash=tx_hash, amount_usd=amount_usd, chain=chain, token=token))
    except Exception as exc:
        logger.error("stake tx verification raised for %s: %s", tx_hash[:12], exc)
        return False


def _response_canonical(result: Any) -> str:
    return json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _bound_response_canonical(capability_id: str, product_id: str, input_payload: Any, result: Any) -> str:
    """Canonical binding the response to the exact request it answers.

    Includes capability_id, product_id and a hash of the request input so a
    provider's signature is valid only for THIS invocation — preventing replay of
    a stale signed response against a different request.
    """
    import hashlib

    input_json = json.dumps(input_payload or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    input_hash = hashlib.sha256(input_json.encode("utf-8")).hexdigest()
    return json.dumps(
        {
            "capability_id": capability_id or "",
            "product_id": product_id or "",
            "input_sha256": input_hash,
            "result": result,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
