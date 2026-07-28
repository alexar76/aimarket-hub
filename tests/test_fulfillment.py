"""A hub must not offer what it cannot run.

Regression cover for the live incident: twelve seeded capabilities priced $0.15-$1.50
were listed for sale on modelmarket.dev while every invoke answered 404, because the
only guard was a name-pattern cleanup and the rows were named respectably
(`code.review@v1`, `legal.review@v1`).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from aimarket_hub.database import HubDatabase
from aimarket_hub.fulfillment import capability_is_fulfillable, has_execution_path
from aimarket_hub.models import Capability


def _cap(**kw) -> Capability:
    base = dict(
        capability_id="x@v1",
        product_id="prod-x",
        name="x",
        version="v1",
        description="",
        input_schema={},
        output_schema={},
        price_per_call_usd=1.0,
        source_hub="local",
    )
    base.update(kw)
    return Capability(**base)


class TestExecutionPath:
    def test_an_invoke_url_is_an_execution_path(self):
        assert has_execution_path("https://provider.example/invoke", "")

    def test_a_static_json_pack_is_an_execution_path(self):
        # Returned verbatim by the invoke handler — e.g. security-rules.sec-feed.
        assert has_execution_path("", '{"rules": []}')

    def test_a_prose_prompt_is_not_an_execution_path(self):
        # prompt_template also holds LLM prompts; only a JSON object is servable.
        assert not has_execution_path("", "You are a helpful assistant.")

    def test_nothing_is_not_an_execution_path(self):
        assert not has_execution_path("", "")
        assert not has_execution_path(None, None)

    def test_whitespace_is_not_a_url(self):
        assert not has_execution_path("   ", "  ")


class TestCapabilityIsFulfillable:
    def test_the_twelve_seeded_rows_are_refused(self):
        """The exact shape found in production: priced, local, no way to run it."""
        assert not capability_is_fulfillable(
            _cap(capability_id="code.review@v1", product_id="prod-code", price_per_call_usd=0.6)
        )

    def test_a_capability_with_a_provider_is_offerable(self):
        assert capability_is_fulfillable(
            _cap(capability_id="skopos.security.posture@v1",
                 invoke_url="https://skopos.modelmarket.dev/invoke")
        )

    def test_a_federated_capability_is_always_offerable(self):
        """The peer owns execution; applying the local rule would unlist the federation."""
        assert capability_is_fulfillable(
            _cap(capability_id="platon.random@v1", source_hub="https://oracles.modelmarket.dev")
        )

    def test_it_accepts_a_plain_dict_row(self):
        assert capability_is_fulfillable(
            {"source_hub": "local", "invoke_url": "https://p/invoke", "prompt_template": ""}
        )
        assert not capability_is_fulfillable(
            {"source_hub": "local", "invoke_url": "", "prompt_template": ""}
        )

    def test_the_demo_flag_is_not_what_decides_it(self):
        """A row can be honest about being a demo and still be unsellable, and vice
        versa. Fulfillability is about execution, not labelling — conflating the two is
        how the seeded rows stayed on sale with is_demo=0."""
        assert not capability_is_fulfillable(_cap(is_demo=True))
        assert capability_is_fulfillable(_cap(is_demo=True, invoke_url="https://p/invoke"))


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmp:
        yield HubDatabase(Path(tmp) / "hub.db")


class TestOfferableCount:
    """`count_offerable` is SQL and `capability_is_fulfillable` is Python — the whole
    point is that they agree, because the manifest publishes the count and lists the
    rows. A drift between them advertises stock the hub then refuses to show."""

    def test_the_count_matches_the_predicate(self, db):
        rows = [
            _cap(capability_id="dead@v1"),
            _cap(capability_id="live@v1", invoke_url="https://p/invoke"),
            _cap(capability_id="pack@v1", prompt_template='{"rules": []}'),
            _cap(capability_id="peer@v1", source_hub="https://peer.example"),
        ]
        for r in rows:
            db.upsert_capability(r)

        assert db.count_capabilities("local") == 3      # the table, dead row included
        assert db.count_offerable("local") == 2         # live + pack
        assert db.count_offerable() == 3                # + the federated one

        by_python = sum(1 for r in rows if capability_is_fulfillable(r))
        assert by_python == db.count_offerable()

    def test_an_empty_hub_offers_nothing(self, db):
        assert db.count_offerable() == 0
        assert db.count_offerable("local") == 0
