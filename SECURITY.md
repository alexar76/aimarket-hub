# Security Policy — aimarket-hub

## Reporting a Vulnerability

**Do not open a public issue for security bugs.**

Email: **alexar76@rambler.ru**

We acknowledge within 48 hours and share a fix timeline.

## Scope

- AIMarket Hub core API, plugins loader, payment channels, federation

## Out of Scope

- Third-party dependencies (report upstream)
- Issues requiring physical access to user hardware
- Social engineering

## Supported Versions

| Version | Supported |
|---------|-----------|
| latest main | yes |
| older tags | best effort |

## Disclosure

Coordinated disclosure preferred. We credit researchers in release notes when permitted.

## Payment channel authorization audit — 2026-10-05

Every `ChannelLedger.open()` and public `open_channel()` now mints a debit secret by
default. `debit`, `hold`, `close`, and `refund` require both a stored hash and the
matching secret. A channel id and a public wallet address never prove ownership.
Existing rows without a secret are frozen for these operations; this change does
not assign secrets to existing rows or move funds. Recovery needs a separate
owner-approved procedure with proof of ownership. Already-authorized holds can
still be captured or released by settlement workers.

The local and federated HTTP 402 paths disclose a balance only when the ledger's
hold result includes it after authentication (insufficient funds). They no longer
perform an unconditional balance lookup after an authorization failure.

Call-site audit: the only application call to this ledger's `open()` is the
`open_channel()` wrapper; the HTTP `/channel/open` route calls that wrapper with
its secure default. There are no application callers of `open_channel(with_secret=False)`
or `ChannelLedger.open(with_secret=False)` in the repository. The two explicit
`with_secret=False` calls are negative tests in `test_channels.py` and
`test_provider_channel_ref.py`: both assert refusal, never an internal exemption.
The other modules named `open_channel` belong to separate ledgers or HTTP clients.
Ledger unit tests now retain and pass the once-returned secret.

Read-only production check at 2026-10-05 14:52:49 UTC (17:52:49 Moscow): apex
`modelmarket-hub`, `/app/data/channels.db`, SQLite URI `mode=ro` and `query_only=ON`.
Predicate: `status='open' AND COALESCE(secret_hash, '')='' AND balance_cents>0`.
Count **0**, total **0 cents ($0)**; oldest/newest/mean age are not applicable.
Before accessing the hub, the oracle-host alerter state was read: no open alerts
or failing checks; its recorded heartbeat was 2026-10-05 12:00:35 UTC. The apex
host has no local alerter state file. No production application or ledger was
imported, migrated, changed, deployed, or used to move funds by this audit.

Separate unresolved issue, deliberately outside this fix:
`attested/attested-memory-hub/patches/aimarket-hub-production.patch` still adds raw
`X-Payment-Channel` forwarding to providers (line 85 at audit time). The main hub
uses `chref-<hmac>` since `e09566a2d`; the attested patch was not changed here.
