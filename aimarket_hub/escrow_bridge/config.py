"""Escrow bridge settings — every read dynamic, every default inert.

The bridge is the only part of the hub that can cause value to move on-chain, so its
configuration is deliberately boring and its defaults are deliberately useless:

    mode OFF                → nothing in the request path changes at all
    strategy "plan"         → the mirror builds and SIMULATES calldata, submits nothing
    no keys anywhere        → a signing key is read from the environment or never seen

Reads go through the functions below (not module constants) so tests and operators can
change one knob without reimporting the hub — the same convention channels.py and
verified_settlement.py already use for their prod gates.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# A non-plan strategy can broadcast a transaction, so it takes a second, deliberate act
# beyond naming the strategy: the operator must type this phrase. It is not a secret —
# it exists so "I set a strategy while exploring" cannot silently become "I authorised
# spending from the hub's key".
SUBMIT_CONFIRM_PHRASE = "i-understand-this-moves-funds"

STRATEGY_PLAN = "plan"
STRATEGY_EXTERNAL = "external"
STRATEGY_ENV = "env"
_STRATEGIES = (STRATEGY_PLAN, STRATEGY_EXTERNAL, STRATEGY_ENV)


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name, default) or "").strip()


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def enabled() -> bool:
    """Master switch. OFF by default: an operator who has not opted in gets today's hub."""
    return _truthy(_env("AIMARKET_ESCROW_BRIDGE_ENABLED", "0"))


def network_id() -> str:
    """Chain the escrow lives on. Empty → chain_net's active network."""
    return _env("AIMARKET_ESCROW_NETWORK")


def contract_address() -> str:
    """Escrow address override. Empty → the address chain_net resolved for the network."""
    return _env("AIMARKET_ESCROW_CONTRACT")


def hub_address() -> str:
    """This hub's EVM address — the ``hub`` field bound into every DebitAuthorization.

    Required in escrow mode and NOT defaulted: the contract binds the signature to one
    hub, so a wrong value here produces authorizations that no contract will accept.
    Guessing it would turn a configuration mistake into a silent, chain-side failure
    discovered only when money should have moved.
    """
    return _env("AIMARKET_ESCROW_HUB_ADDRESS")


def submit_strategy() -> str:
    """``plan`` (default, submits nothing) | ``external`` (operator signer) | ``env`` (key).

    An unrecognised value falls back to ``plan`` rather than raising: a typo in a
    deployment variable must not be able to escalate what the mirror is allowed to do,
    and it must not take the hub down either.
    """
    raw = _env("AIMARKET_ESCROW_SUBMIT_STRATEGY", STRATEGY_PLAN).lower()
    return raw if raw in _STRATEGIES else STRATEGY_PLAN


def submit_confirmed() -> bool:
    """Whether the operator typed the confirmation phrase for a value-moving strategy."""
    return _env("AIMARKET_ESCROW_SUBMIT_CONFIRM") == SUBMIT_CONFIRM_PHRASE


def signer_url() -> str:
    """External signer endpoint (``external`` strategy). No key enters this process."""
    return _env("AIMARKET_ESCROW_SIGNER_URL")


def signer_token() -> str:
    """Bearer token for the external signer, if it requires one."""
    return _env("AIMARKET_ESCROW_SIGNER_TOKEN")


def private_key() -> str:
    """Hub signing key for the ``env`` strategy. Read here and nowhere else.

    Never logged, never persisted, never included in an error message — the store and
    the mirror only ever see the transaction they asked to have signed.
    """
    return _env("AIMARKET_ESCROW_PRIVATE_KEY")


def db_path() -> str:
    """The bridge's own SQLite file. Empty → beside the channel ledger's database."""
    return _env("AIMARKET_ESCROW_BRIDGE_DB_PATH")


def rpc_timeout_s() -> float:
    try:
        return max(1.0, float(_env("AIMARKET_ESCROW_RPC_TIMEOUT_S", "10") or 10))
    except ValueError:
        return 10.0


def max_authorization_ttl_s() -> int:
    """Upper bound on how far in the future a DebitAuthorization deadline may sit.

    A deadline is the buyer's protection: it caps how long the hub may hold a signed
    claim on their money. An unbounded deadline turns one signature into a standing
    licence, so an authorization asking for more than this is refused.
    """
    try:
        return max(60, int(float(_env("AIMARKET_ESCROW_AUTH_MAX_TTL_S", "86400") or 86400)))
    except ValueError:
        return 86400


@dataclass(frozen=True)
class SubmitPolicy:
    """Resolved answer to "may this process broadcast, and how?"."""

    strategy: str
    confirmed: bool
    reason: str = ""

    @property
    def may_broadcast(self) -> bool:
        return self.strategy != STRATEGY_PLAN and self.confirmed and not self.reason


def submit_policy() -> SubmitPolicy:
    """Resolve the submission policy, refusing anything under-configured.

    Every path that could broadcast has to come through here, so the "can this move
    money" decision exists in exactly one place and reads the same in tests as in prod.
    """
    strategy = submit_strategy()
    if strategy == STRATEGY_PLAN:
        return SubmitPolicy(strategy=strategy, confirmed=False)
    if not submit_confirmed():
        return SubmitPolicy(
            strategy=strategy, confirmed=False,
            reason=(
                f"strategy {strategy!r} can broadcast — set "
                f"AIMARKET_ESCROW_SUBMIT_CONFIRM={SUBMIT_CONFIRM_PHRASE!r} to allow it"
            ),
        )
    if strategy == STRATEGY_EXTERNAL and not signer_url():
        return SubmitPolicy(
            strategy=strategy, confirmed=True,
            reason="strategy 'external' needs AIMARKET_ESCROW_SIGNER_URL",
        )
    if strategy == STRATEGY_ENV and not private_key():
        return SubmitPolicy(
            strategy=strategy, confirmed=True,
            reason="strategy 'env' needs AIMARKET_ESCROW_PRIVATE_KEY",
        )
    return SubmitPolicy(strategy=strategy, confirmed=True)


def describe() -> dict[str, object]:
    """Operator-facing snapshot. Deliberately reports NO secret material."""
    policy = submit_policy()
    return {
        "enabled": enabled(),
        "network": network_id() or "(chain_net active)",
        "contract": contract_address() or "(chain_net registry)",
        "hub_address_set": bool(hub_address()),
        "strategy": policy.strategy,
        "may_broadcast": policy.may_broadcast,
        "blocked_reason": policy.reason,
        "signer_url_set": bool(signer_url()),
        "private_key_set": bool(private_key()),
    }
