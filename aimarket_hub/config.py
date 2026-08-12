"""Hub configuration — env vars, defaults, paths."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from aimarket_hub import __version__


# Anvil/Hardhat dev-mnemonic accounts and deterministic dev contract addresses.
# Their private keys are public, so anything settling there is sweepable by anyone.
# Duplicated from security/prod_startup_guard.py on purpose: the standalone hub image
# does NOT ship the `security` package (verified on the modelmarket.dev container), so
# without a copy here a dev recipient would sail past every check in that deployment.
_WELL_KNOWN_DEV_ADDRESSES: frozenset[str] = frozenset(
    a.lower()
    for a in (
        "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
        "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
        "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC",
        "0x90F79bf6EB2c4f870365E785982E1f101E93b906",
        "0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65",
        "0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc",
        "0x976EA74026E726554dB657fA54763abd0C3a0aa9",
        "0x14dC79964da2C08b23698B3D3cc7Ca32193d9955",
        "0x23618e81E3f5cdF7f54C3d65f7FBc0aBf5B21E8f",
        "0xa0Ee7A142d267C1f36714E4a8F75612F20a79720",
        "0x5FbDB2315678afecb367f032d93F642f64180aa3",
        "0xe7f1725E7734CE288F8367e1Bb143E90bb3F0512",
        "0x9fE46736679d2D9a65F0992F2272dE9f3c7fa6e0",
        "0xCf7Ed3AccA5a467e9e704C703E8D87F634fB0Fc9",
        "0xDc64a140Aa3E981100a9becA4E685f962f0cF6C9",
    )
)


def is_dev_chain_address(address: str) -> bool:
    """True for an Anvil/Hardhat address that must never receive real funds."""
    return (address or "").strip().lower() in _WELL_KNOWN_DEV_ADDRESSES


@dataclass
class HubConfig:
    """Configuration for an AIMarket Hub instance."""

    # Identity
    hub_name: str = field(default_factory=lambda: os.getenv("AIMARKET_HUB_NAME", "AIMarket Hub"))
    hub_url: str = field(default_factory=lambda: os.getenv("AIMARKET_HUB_URL", "http://localhost:9083"))
    # Taken from the package, not restated. This field existed at "3.0.0" and was never
    # read by anything — api.py carried its own three literals — so the number the hub
    # reported in /health, the OpenAPI doc and .well-known could drift from the version
    # actually shipped, and had. One source, four readers.
    hub_version: str = __version__

    # Database (SQLite path or PostgreSQL URL)
    db_path: str = field(default_factory=lambda: os.getenv("AIMARKET_DB_PATH", "data/hub.db"))
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", ""))

    # Federation
    crawl_interval_s: int = field(default_factory=lambda: int(os.getenv("AIMARKET_CRAWL_INTERVAL_S", "3600")))
    routing_fee_bps: int = field(default_factory=lambda: int(os.getenv("AIMARKET_ROUTING_FEE_BPS", "100")))
    min_trust_score: float = field(default_factory=lambda: float(os.getenv("AIMARKET_MIN_TRUST_SCORE", "0.3")))
    max_crawl_depth: int = field(default_factory=lambda: int(os.getenv("AIMARKET_MAX_CRAWL_DEPTH", "3")))

    # Charity — the Hub's altruistic tithe to ITS OWN bound lottery (a machine UBI for
    # agents). The DONOR owns this: how generous it is, whether it is on, and WHICH
    # lottery it funds. The lottery's economy engine reads these (as HUB_TITHE_BPS /
    # HUB_CHARITY_ENABLED / HUB_LOTTERY_ADDRESS) and enforces the anti-redirect binding,
    # so the Hub funds only its single bound lottery — and nothing if none is deployed.
    # See lottery/ (config/sponsor.yaml binding + relayer/ailottery_relayer/sponsor.py).
    charity_enabled: bool = field(default_factory=lambda: os.getenv("AIMARKET_CHARITY_ENABLED", "1").strip().lower() not in ("0", "false", "no"))
    charity_tithe_bps: int = field(default_factory=lambda: int(os.getenv("AIMARKET_CHARITY_TITHE_BPS", "2000")))
    charity_lottery_address: str = field(default_factory=lambda: os.getenv("AIMARKET_CHARITY_LOTTERY_ADDRESS", ""))
    charity_lottery_chain_id: int = field(default_factory=lambda: int(os.getenv("AIMARKET_CHARITY_LOTTERY_CHAIN_ID", "1")))
    # The HUB PUSHES the accrued tithe to its bound lottery every N hours (the lottery
    # never pulls). Default 24h — a daily machine-UBI dividend: frequent enough to feel
    # alive, long enough that gas/overhead stays negligible vs. a day of routing fees.
    # Only fires when a lottery is connected (bound + has code) AND charity_enabled.
    charity_interval_hours: int = field(default_factory=lambda: int(os.getenv("AIMARKET_CHARITY_INTERVAL_HOURS", "24")))

    # Seed list — federation crawl roots (well-known URLs). Falls back to the
    # committed federation_seeds.json so ecosystem nodes (e.g. Platon) are
    # discovered out-of-the-box without hand-editing any node list.
    seed_list: list[str] = field(default_factory=lambda: _parse_seed_list())

    # Operator-vouched public keys for seed peers, keyed by base hub URL AND
    # well-known URL. When a seed advertises exactly its pinned key the crawler
    # trusts + indexes it on first contact instead of waiting for manual
    # approval (see crawler._crawl_one). A mismatch is treated as untrusted.
    seed_pubkeys: dict[str, str] = field(default_factory=lambda: _parse_seed_pubkeys())

    # Auto-crawl scheduler — periodic federation crawl honouring crawl_interval_s.
    # Disabled by setting AIMARKET_AUTO_CRAWL to 0/false/no. Runs only when the
    # seed list is non-empty; uses an in-process lock so crawls never overlap.
    auto_crawl: bool = field(default_factory=lambda: os.getenv("AIMARKET_AUTO_CRAWL", "1").strip().lower() not in ("0", "false", "no"))
    crawl_initial_delay_s: float = field(default_factory=lambda: float(os.getenv("AIMARKET_CRAWL_INITIAL_DELAY_S", "5")))

    # Reject indexing a peer manifest whose `generated_at` is older than this many
    # seconds — mitigates replay of a captured, validly-signed manifest by an
    # attacker who hijacks a plaintext-HTTP seed. 0 disables; missing/unparseable
    # timestamps are allowed (lenient). Default 7 days.
    manifest_max_age_s: int = field(default_factory=lambda: int(os.getenv("AIMARKET_MANIFEST_MAX_AGE_S", "604800")))

    # Keys
    signing_key_path: str = field(default_factory=lambda: os.getenv("AIMARKET_SIGNING_KEY_PATH", "data/hub_signing_key"))

    # HTTP
    request_timeout_s: float = field(default_factory=lambda: float(os.getenv("AIMARKET_REQUEST_TIMEOUT_S", "30")))

    # MASTER crypto switch — OFF by default across the whole ecosystem. When off,
    # payment channels, on-chain verification, escrow, and NFT minting are all
    # disabled and capabilities are served on a free tier; manifest/receipt signing
    # and sandbox trials keep working. Same env var + default + truthy rule as every
    # other AICOM component (AIFACTORY_CRYPTO_ENABLED). Set to 1/true/yes/on to enable.
    crypto_enabled: bool = field(default_factory=lambda: os.getenv("AIFACTORY_CRYPTO_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on"))

    # Production mode — mirrors security.prod_startup_guard / channels._is_production_mode.
    # Read here so payment readiness can be answered from config alone.
    production_mode: bool = field(default_factory=lambda: os.getenv("AIFACTORY_PROD", "").strip() == "1")

    # Payment — multi-chain
    payment_chains: list[str] = field(default_factory=lambda: _parse_chains())
    payment_tokens: list[str] = field(default_factory=lambda: _parse_tokens())
    payment_recipient: str = field(default_factory=lambda: os.getenv("AIMARKET_PAYMENT_RECIPIENT", ""))
    payment_testnet: bool = field(default_factory=lambda: os.getenv("AIFACTORY_PAYMENT_TESTNET", "1") == "1")
    payment_verify_stub: bool = field(default_factory=lambda: os.getenv("AIFACTORY_PAYMENT_VERIFY_STUB", "0") == "1")
    payment_min_confirmations: int = field(default_factory=lambda: int(os.getenv("AIFACTORY_PAYMENT_MIN_CONFIRMATIONS", "2")))

    # Escrow contracts (set after deployment)
    escrow_evm_address: str = field(default_factory=lambda: os.getenv("AIMARKET_ESCROW_EVM_ADDRESS", ""))
    escrow_solana_program_id: str = field(default_factory=lambda: os.getenv("AIMARKET_ESCROW_SOLANA_PROGRAM_ID", "9BcJEAQCeFrPunKQ16itbaAzpw9A4zMHYPQxNxEAZUXR"))

    # Hub bond — **declarative / experimental** for the v2 reference impl.
    #
    # These fields advertise the operator's economic stake in the federation
    # `.well-known/ai-market.json` payload (peer hubs use them to weight trust
    # score). They are NOT yet enforced on-chain: there is no escrow contract
    # that holds the bond, and `setHubAuthorization` does not require a
    # deposit before flipping the flag. The slashing path is roadmap, not
    # protocol-mandatory, so do NOT treat the presence of these values as
    # "this hub has stake at risk".
    #
    # See spec.md §5.3 (Bond Requirement) for the future enforcement model.
    hub_bond_usd: float = field(default_factory=lambda: float(os.getenv("AIMARKET_HUB_BOND_USD", "100")))
    hub_bond_token: str = field(default_factory=lambda: os.getenv("AIMARKET_HUB_BOND_TOKEN", "USDT"))
    hub_bond_chain: str = field(default_factory=lambda: os.getenv("AIMARKET_HUB_BOND_CHAIN", "base"))
    hub_bond_enforced: bool = field(default_factory=lambda: os.getenv("AIMARKET_HUB_BOND_ENFORCED", "0") == "1")

    # Factory seed (demo mode)
    factory_seed_usd: float = field(default_factory=lambda: float(os.getenv("AIMARKET_FACTORY_SEED_USD", "0")))

    def sells_on_behalf_of(self, peer_url: str) -> bool:
        """Whether THIS hub is the seller of record for a federated peer.

        Default False, and that default is the whole point. The federated economics were
        broker-shaped: the peer bills the buyer, this hub forwards the channel and takes
        ``routing_fee_bps``. For a third-party peer that is correct and must not change.

        But the peers that actually exist here do not bill at all — ``oracle_core`` contains no
        402, no price check, nothing — so 42 of the 47 catalogued capabilities were free to call
        while advertising a price, and the 1% fee was a broker's cut of a sale that never
        happened. All the payment machinery (channels, holds, the ledger, escrow) lives in this
        hub, and putting it into seventeen oracles so they could bill their own operator would
        be absurd. So for those peers this hub sells directly: it holds and captures the full
        list price, and takes no routing fee on top, because it is not standing between two
        parties.

        This must be DECLARED rather than inferred. "The peer answered 200" does not mean the
        peer did not charge — a peer that invoices out of band answers 200 too, and billing the
        full price then would charge the buyer twice.

        ``AIMARKET_SELLS_FOR`` is a comma-separated list of peer URLs, matched on the
        scheme+host+path prefix so ``https://oracles.example/family`` covers the capabilities
        served under it.
        """
        peer = (peer_url or "").strip().rstrip("/").lower()
        if not peer:
            return False
        for entry in os.getenv("AIMARKET_SELLS_FOR", "").split(","):
            own = entry.strip().rstrip("/").lower()
            if own and (peer == own or peer.startswith(own + "/")):
                return True
        return False

    def payment_readiness(self) -> list[str]:
        """Reasons this hub cannot verify a real payment. Empty list == ready.

        `payment_verify_stub=0` alone is NOT enough: `channels._open_channel`
        only calls the on-chain verifier when stub is off **and** the process
        runs in production mode. With stub off and `AIFACTORY_PROD` unset every
        deposit is refused outright (fail-closed via `_allow_demo_credit`) —
        so the old `not stub` flag advertised "payments configured" on a hub
        that in fact takes nothing. Every interlock below must hold before this
        hub tells peers it takes money.
        """
        missing: list[str] = []
        if not self.crypto_enabled:
            missing.append("AIFACTORY_CRYPTO_ENABLED is off — no payment surface at all")
        if self.payment_verify_stub:
            missing.append("AIFACTORY_PAYMENT_VERIFY_STUB=1 — any tx_hash is accepted unverified")
        if not self.production_mode:
            missing.append("AIFACTORY_PROD is not '1' — deposits are refused, nothing is verified")
        if not self.payment_recipient.strip():
            missing.append("AIMARKET_PAYMENT_RECIPIENT is empty — deposits have nowhere to settle")
        elif is_dev_chain_address(self.payment_recipient):
            missing.append(
                f"AIMARKET_PAYMENT_RECIPIENT={self.payment_recipient} is an Anvil/Hardhat dev "
                "address — its private key is public and every deposit is sweepable by anyone"
            )
        if self.escrow_evm_address.strip() and is_dev_chain_address(self.escrow_evm_address):
            missing.append(
                f"AIMARKET_ESCROW_EVM_ADDRESS={self.escrow_evm_address} is a dev-chain address — "
                "no escrow contract lives there on a real chain"
            )
        # The one setting that collapses the separation between the two rails. KI-11 case 2:
        # if the custodial ledger and an escrow contract channel are backed by the same
        # money, the contract's `usedAmount` stays 0 however much the ledger consumed, so
        # refundChannel/expireChannel hands a fully-consumed deposit back IN FULL. Pointing
        # the settlement recipient at the escrow address is how that happens by accident —
        # every deposit then lands in the contract while the hub books it as a plain
        # transfer it fully controls.
        if (
            self.payment_recipient.strip()
            and self.escrow_evm_address.strip()
            and self.payment_recipient.strip().lower() == self.escrow_evm_address.strip().lower()
        ):
            missing.append(
                f"AIMARKET_PAYMENT_RECIPIENT and AIMARKET_ESCROW_EVM_ADDRESS are both "
                f"{self.payment_recipient} — the custodial ledger and the escrow contract "
                "would be backed by the same money, and a refund would return a deposit "
                "the ledger has already spent (see KI-11). They must be different addresses"
            )
        # Not a missing interlock but an active bypass: demo credit on an otherwise
        # production hub hands out channels for a tx_hash nobody checked.
        if os.getenv("AIMARKET_ALLOW_DEMO_CREDIT", "").strip() == "1":
            missing.append("AIMARKET_ALLOW_DEMO_CREDIT=1 — unverified deposits are credited")
        return missing

    @property
    def payment_ready(self) -> bool:
        """True only when a deposit would actually be verified on-chain."""
        return not self.payment_readiness()

    # Backward compat
    @property
    def payment_chain(self) -> str:
        return self.payment_chains[0] if self.payment_chains else "base"

    @property
    def payment_token(self) -> str:
        return self.payment_tokens[0] if self.payment_tokens else "USDT"

    def db_dir(self) -> Path:
        return Path(self.db_path).parent


def _seeds_file_path() -> Path:
    env = os.getenv("AIMARKET_SEEDS_FILE", "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parent / "federation_seeds.json"


def _load_seed_file() -> list[dict]:
    """Load committed federation seeds. Fault-tolerant: returns [] on any error."""
    path = _seeds_file_path()
    try:
        if path.is_file():
            data = json.loads(path.read_text())
            seeds = data.get("seeds") if isinstance(data, dict) else data
            return [s for s in (seeds or []) if isinstance(s, dict)]
    except Exception:
        return []
    return []


def _parse_seed_list() -> list[str]:
    raw = os.getenv("AIMARKET_SEED_LIST", "")
    if raw:
        return [u.strip() for u in raw.split(",") if u.strip()]
    # Fall back to the committed seeds file so discovery works by default.
    return [
        s["well_known_url"].strip()
        for s in _load_seed_file()
        if isinstance(s.get("well_known_url"), str) and s["well_known_url"].strip()
    ]


def _pin(out: dict[str, str], url: str, key: str) -> None:
    """Pin a key under both the well-known URL and its base hub URL."""
    url, key = url.strip(), key.strip()
    if not url or not key:
        return
    out[url] = key
    out[url.rsplit("/.well-known/", 1)[0]] = key


def _parse_seed_pubkeys() -> dict[str, str]:
    """Operator-vouched public keys for trusted-on-first-contact seed indexing.

    Sources, later overriding earlier:
      1. The committed federation_seeds.json `public_key` fields.
      2. AIMARKET_SEED_PUBKEYS env — JSON object {url: key} or `url=key,url=key`.
    """
    out: dict[str, str] = {}
    for s in _load_seed_file():
        wk = s.get("well_known_url") or ""
        key = s.get("public_key") or ""
        if isinstance(wk, str) and isinstance(key, str):
            _pin(out, wk, key)
    raw = os.getenv("AIMARKET_SEED_PUBKEYS", "").strip()
    if raw:
        parsed = False
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if isinstance(k, str) and isinstance(v, str):
                        _pin(out, k, v)
                parsed = True
        except json.JSONDecodeError:
            parsed = False
        if not parsed:
            for pair in raw.split(","):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    _pin(out, k, v)
    return out


def _parse_chains() -> list[str]:
    raw = os.getenv("AIMARKET_PAYMENT_CHAINS", os.getenv("AIMARKET_PAYMENT_CHAIN", "base,ethereum,arbitrum,optimism,polygon"))
    return [c.strip() for c in raw.split(",") if c.strip()]


def _parse_tokens() -> list[str]:
    raw = os.getenv("AIMARKET_PAYMENT_TOKENS", os.getenv("AIMARKET_PAYMENT_TOKEN", "USDT,USDC,ETH"))
    return [t.strip() for t in raw.split(",") if t.strip()]
