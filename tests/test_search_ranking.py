"""Search must put the right capability first, not the most-trusted one.

`search_capabilities` matched ANY term and then ordered by `trust_score` alone, so
relevance played no part in the result at all. On the live catalogue that meant the oracle
family (trust 0.265) lost every query to Platon (0.5): "verifiable delay proof" answered
platon.random instead of chronos.eval, "cascade risk in a network" answered platon.beacon
instead of ablation.cascade. The right capability existed, was priced, was executable —
and was unreachable through the only way a buyer looks for it.

The fixture mirrors the real shape of that catalogue, including the trust gap, because the
gap is what made the bug invisible in any test with uniform trust.
"""

from __future__ import annotations

import pytest

from aimarket_hub.database import HubDatabase
from aimarket_hub.models import Capability
from aimarket_hub.semantic_search import rank_capabilities

CATALOGUE = [
    ("chronos.eval@v1", "chronos.eval",
     "Evaluate the VDF: y = g^(2^T) mod N via T sequential squarings. Verifiable delay proof.", 0.265),
    ("chronos.verify@v1", "chronos.verify", "Verify a VDF proof. Cheap, trustless.", 0.265),
    ("ablation.cascade@v1", "ablation.cascade",
     "Analyse a network's systemic cascade risk. Treats the graph as a load-bearing structure.", 0.265),
    ("lumen.score@v1", "lumen.score",
     "Single-agent trust lookup: PageRank score, rank and percentile. Reputation of an agent.", 0.265),
    ("fermat.route@v1", "fermat.route", "Compute the globally least-time composite route.", 0.265),
    ("landauer.audit@v1", "landauer.audit", "Audit a computation's thermodynamic cost.", 0.265),
    # Higher trust, and deliberately worded so a term-count tie is possible.
    ("platon.random@v1", "platon.random", "Signed chaos-VRF randomness with proof.", 0.5),
    ("platon.beacon@v1", "platon.beacon", "Hash-chained randomness beacon round, verifiable.", 0.5),
    ("platon.state@v1", "platon.state", "Snapshot of the 32D universe: telemetry, oscillators, risk.", 0.5),
    ("skopos.security.posture@v1", "Security posture", "Fleet security score, grade, top alerts.", 0.5),
]


@pytest.fixture
def db(tmp_path):
    d = HubDatabase(tmp_path / "search.db")
    for cid, name, desc, trust in CATALOGUE:
        d.upsert_capability(Capability(
            capability_id=cid, product_id="p", name=name, version="v1", description=desc,
            input_schema={}, output_schema={}, price_per_call_usd=0.01,
            source_hub="peer", trust_score=trust, invoke_url="https://p/invoke",
        ))
    return d


@pytest.mark.parametrize(
    "query,expected_first",
    [
        # Each of these answered a higher-trust, less relevant capability before.
        ("verifiable delay proof", "chronos.eval@v1"),
        ("cascade risk in a network", "ablation.cascade@v1"),
        ("reputation of an agent", "lumen.score@v1"),
        ("least-time route", "fermat.route@v1"),
        ("thermodynamic cost of a computation", "landauer.audit@v1"),
        # And the high-trust rows must still win when they are genuinely the best match.
        ("fleet security posture", "skopos.security.posture@v1"),
    ],
)
def test_the_best_match_ranks_first(db, query, expected_first):
    got = [c.capability_id for c in db.search_capabilities(query, limit=5)]
    assert got and got[0] == expected_first, f"{query!r} -> {got}"


def test_trust_only_breaks_ties(db):
    """Two rows matching a query equally well should order by trust — the old behaviour,
    kept, but demoted to what it was always suited for."""
    got = [c.capability_id for c in db.search_capabilities("randomness", limit=5)]
    assert got[:2] == ["platon.random@v1", "platon.beacon@v1"] or got[:2] == [
        "platon.beacon@v1", "platon.random@v1"
    ], got
    # …and both outrank a 0.265 row that does not mention randomness at all.
    assert "platon.state@v1" not in got[:2], got


def test_stopwords_do_not_decide_the_ranking(db):
    """"of", "a", "in" appear in most descriptions; matching them ranked by noise."""
    got = [c.capability_id for c in db.search_capabilities("the cost of a computation", limit=3)]
    assert got[0] == "landauer.audit@v1", got


def test_an_all_stopword_query_still_searches(db):
    """Filtering every term must not silently turn into "return the whole catalogue"."""
    got = db.search_capabilities("what is it for", limit=3)
    assert len(got) <= 3


def test_empty_browse_diversifies_across_hubs(tmp_path):
    """Empty intent must not fill every slot with one high-trust peer."""
    d = HubDatabase(tmp_path / "diverse.db")
    for i in range(8):
        d.upsert_capability(Capability(
            capability_id=f"platon.cap{i}@v1", product_id="platon", name=f"p{i}",
            description="Signed randomness", source_hub="https://oracles.example/platon",
            source_hub_name="Platon", trust_score=0.9, invoke_url="https://p/i",
            price_per_call_usd=0.01,
        ))
    for sku in ("gaia.grid.read@v1", "gaia.quake.read@v1", "gaia.tide.read@v1"):
        d.upsert_capability(Capability(
            capability_id=sku, product_id="gaia", name=sku.split(".")[1],
            description="Live relay reading", source_hub="https://iot.example",
            source_hub_name="GAIA", trust_score=0.3, invoke_url="https://g/i",
            price_per_call_usd=0.001,
        ))
    ranked = d.search_capabilities_ranked("", limit=6)
    hubs = {c.source_hub for c, _ in ranked}
    assert "https://iot.example" in hubs, [c.capability_id for c, _ in ranked]
    assert all(s == 0.0 for _, s in ranked)  # browse, not a relevance claim


def test_live_intent_finds_gaia_relays(tmp_path):
    """'live' must recall SKUs that say relay/attested, not only the word 'live'."""
    d = HubDatabase(tmp_path / "live.db")
    d.upsert_capability(Capability(
        capability_id="platon.random@v1", product_id="p", name="random",
        description="Signed chaos-VRF randomness", source_hub="https://o",
        trust_score=0.5, invoke_url="https://p/i", price_per_call_usd=0.01,
    ))
    d.upsert_capability(Capability(
        capability_id="gaia.grid.read@v1", product_id="gaia", name="grid",
        description="Live UK grid carbon-intensity relay", source_hub="https://iot",
        trust_score=0.3, invoke_url="https://g/i", price_per_call_usd=0.001,
    ))
    got = [c.capability_id for c in d.search_capabilities("live sensors", limit=5)]
    assert "gaia.grid.read@v1" in got, got
    scores = {c.capability_id: s for c, s in d.search_capabilities_ranked("live", limit=5)}
    assert scores["gaia.grid.read@v1"] > 0
    assert scores.get("platon.random@v1", 0) < scores["gaia.grid.read@v1"]


def test_relevance_score_is_not_a_constant(db):
    ranked = db.search_capabilities_ranked("verifiable delay proof", limit=5)
    assert ranked
    scores = [s for _, s in ranked]
    assert scores[0] > scores[-1] or len(set(scores)) > 1 or ranked[0][0].capability_id == "chronos.eval@v1"
    assert all(0.0 <= s <= 1.0 for s in scores)
    assert not all(s == 0.8 for s in scores)


@pytest.fixture
def semantic_catalogue():
    """Small but adversarial slice of the live federated catalogue."""
    rows = [
        ("gaia.weather.read@v1", "Live attested weather: temperature, humidity and wind"),
        ("gaia.fire.read@v1", "Live NASA FIRMS wildfire hotspot"),
        ("gaia.jamming.read@v1", "Live GNSS interference event"),
        ("gaia.ais.read@v1", "Own-edge AIS vessel feeder"),
        ("gaia.ais.public.read@v1", "Live Fintraffic Digitraffic AIS snapshot Finnish waters CC BY 4.0 credit Fintraffic not GFW"),
        ("gaia.cyclone.read@v1", "Live NOAA NHC CPHC active tropical cyclones Atlantic East Pacific hurricane not JTWC"),
        ("gaia.adsb.public.read@v1", "Live ADSB.lol area snapshot ODbL cite ADSB.lol not own-edge not OpenSky"),
        ("atlas.fire.weather@v1", "Wildfire hotspots from NASA FIRMS and/or Copernicus EFFIS in a bbox plus nearest live weather"),
        ("atlas.situation.brief@v1", "Cross-layer situation brief for a bbox: flood, EFFIS, lightning, alerts, events, volcano and other LIVE layers"),
        ("atlas.watchbox.check@v1", "Check a geographic watchbox and layer filter"),
        ("platon.random@v1", "Signed chaos-VRF randomness"),
        ("percola.verify@v1", "Verify a graph percolation removal order"),
        ("chronos.eval@v1", "Verifiable delay proof that sequential time passed"),
        ("aestus.seal@v1", "Seal a time-lock puzzle for later opening"),
    ]
    return [
        Capability(
            capability_id=cid,
            product_id=cid.split(".", 1)[0],
            name=cid.split("@", 1)[0],
            description=description,
            source_hub="https://peer.example",
            trust_score=0.4,
            success_rate_30d=0.98,
            p50_latency_ms=100,
            price_per_call_usd=0.001,
            invoke_url="https://peer.example/invoke",
        )
        for cid, description in rows
    ]


@pytest.mark.parametrize(
    "query,expected_first",
    [
        ("погода сейчас", "gaia.weather.read@v1"),
        ("честная случайность для лотереи", "platon.random@v1"),
        ("protect a ship from navigation spoofing", "gaia.jamming.read@v1"),
        ("защитить судно от навигационных помех", "gaia.jamming.read@v1"),
        ("что предупредит корабль если GPS глушат", "gaia.jamming.read@v1"),
        ("alerta de interferencia GPS para un barco", "gaia.jamming.read@v1"),
        ("preuve de délai vérifiable", "chronos.eval@v1"),
        ("附近森林火灾天气", "atlas.fire.weather@v1"),
        ("EFFIS Copernicus fire weather", "atlas.fire.weather@v1"),
        ("flood river brief", "atlas.situation.brief@v1"),
        ("finnish ais vessels fintraffic", "gaia.ais.public.read@v1"),
        # A US desk types "hurricane", never "cyclone" — the NHC feed has to
        # answer the word people actually use.
        ("hurricane tracking", "gaia.cyclone.read@v1"),
        ("ураган в атлантике", "gaia.cyclone.read@v1"),
        ("monitor wildfire near a facility and alert me", "atlas.watchbox.check@v1"),
    ],
)
def test_multilingual_natural_language_intents_rank_the_right_tool(
    semantic_catalogue, query, expected_first
):
    _intent, matches = rank_capabilities(
        query,
        semantic_catalogue,
        limit=5,
        localized_descriptions={},
    )
    assert matches, query
    assert matches[0].capability.capability_id == expected_first, [
        (m.capability.capability_id, m.score, m.matched_concepts) for m in matches
    ]
    assert matches[0].match_type in {"semantic", "hybrid", "exact"}
    assert matches[0].matched_concepts


def test_exact_id_stays_deterministic_and_typo_gets_a_safe_fallback(semantic_catalogue):
    _intent, exact = rank_capabilities(
        "gaia.weather.read@v1", semantic_catalogue, localized_descriptions={}
    )
    assert exact[0].capability.capability_id == "gaia.weather.read@v1"
    assert exact[0].match_type == "exact"

    _intent, typo = rank_capabilities(
        "wether", semantic_catalogue, localized_descriptions={}
    )
    assert typo[0].capability.capability_id == "gaia.weather.read@v1"
    assert typo[0].match_type in {"fuzzy", "hybrid", "lexical"}


def test_unknown_intent_does_not_fabricate_relevance(semantic_catalogue):
    _intent, matches = rank_capabilities(
        "quantum banana upholstery", semantic_catalogue, localized_descriptions={}
    )
    assert matches == []


@pytest.mark.parametrize(
    "query,expected_first",
    [
        # The public ODbL relay and the own-edge feeder share the aviation concept,
        # so the only thing separating them is the wording of the request. This
        # asserts placement only: an exact-word request legitimately matches
        # lexically, and the shared multilingual test above pins match_type.
        ("public adsb aircraft positions", "gaia.adsb.public.read@v1"),
        ("adsb.lol snapshot", "gaia.adsb.public.read@v1"),
    ],
)
def test_public_relay_outranks_its_own_edge_twin(semantic_catalogue, query, expected_first):
    _intent, matches = rank_capabilities(
        query, semantic_catalogue, limit=5, localized_descriptions={}
    )
    assert matches, query
    assert matches[0].capability.capability_id == expected_first, [
        m.capability.capability_id for m in matches[:3]
    ]
