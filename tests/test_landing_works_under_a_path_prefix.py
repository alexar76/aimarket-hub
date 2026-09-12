"""The landing must localise when the hub is mounted under a path prefix.

`independentai.network/hub/` runs the same hub, and its whole UI rendered as raw keys —
NAV_DEVS, S_INV, PENDING_TITLE, ASSAY_FAIL — because the page fetched the dictionary from
the absolute `/hub-ui-i18n.json`. Under a prefix that is a 404, the fetch fails, and every
`data-i18n` element keeps its placeholder. It was invisible on modelmarket.dev, which serves
the hub at the apex where the absolute path happens to resolve — a bug only the second
deployment could show.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / "terminal-home.html"


def _page() -> str:
    return PAGE.read_text(errors="ignore")


def _code_only() -> str:
    """The page with // comments blanked.

    Without this the assertions match the old path quoted in the comment that explains the
    fix — a source-inspection test passing or failing on its own prose. Block comments and
    URLs are left alone: only a `//` that starts a line is a comment here.
    """
    out = []
    for line in _page().splitlines():
        out.append("" if line.lstrip().startswith("//") else line)
    return "\n".join(out)


def test_the_dictionary_is_not_fetched_from_the_site_root():
    assert "'/hub-ui-i18n.json'" not in _code_only(), (
        "an absolute path 404s for every hub mounted under a prefix, and the page then "
        "renders its i18n keys instead of text"
    )


def test_the_dictionary_is_resolved_against_the_document():
    t = _page()
    assert "new URL('hub-ui-i18n.json', document.baseURI)" in t, (
        "resolve the dictionary against document.baseURI so it works at the apex and under "
        "a prefix alike"
    )


def test_no_other_same_origin_asset_is_pinned_to_the_root():
    """Any other absolute-root asset would break the same way under a prefix.

    The API paths are deliberately excluded: those are proxied at the root on every
    deployment we run, and are verified separately.
    """
    t = _code_only()
    absolute = set(re.findall(r"""fetchJson\(\s*['"](/[^'"]+)['"]""", t))
    absolute |= set(re.findall(r"""fetch\(\s*['"](/[^'"]+)['"]""", t))
    offenders = sorted(
        p for p in absolute
        if not p.startswith(("/ai-market/", "/api/", "/.well-known/"))
    )
    assert not offenders, (
        f"these are pinned to the site root and would 404 under a path prefix: {offenders}"
    )


def test_every_data_i18n_key_the_page_uses_exists_in_the_dictionary():
    """The other way this renders as keys: a template key with no translation."""
    import json

    dictionary = json.loads((PAGE.parent / "hub-ui-i18n.json").read_text())
    ui = dictionary.get("ui") or {}
    en = ui.get("en") or {}
    used = set(re.findall(r'data-i18n(?:-html)?="([a-z0-9_]+)"', _page()))
    assert used, "no data-i18n keys found — the scan is looking at the wrong markup"
    missing = sorted(used - set(en))
    assert not missing, f"used in the page but absent from ui.en: {missing}"


# ── White-labelling ─────────────────────────────────────────────────────────────────────

def test_a_third_party_hub_does_not_show_our_brand(tmp_path, monkeypatch):
    """`independentai.network/hub/` rendered OUR wordmark in the hero, twice the size of its
    own name, and its tab title said modelmarket.dev.

    Two separate leaks. The hero wordmark is split across spans —
    `<span class="grad">model</span>market<span class="grad">.dev</span>` — so it survived
    every brand rewrite. And the client-side translator sets document.title from the
    dictionary, whose `page_title` carries the reference brand, so the page re-branded itself
    back a moment after loading and undid the server's <title> rewrite.
    """
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AIMARKET_DB_PATH", str(tmp_path / "wl.db"))
    monkeypatch.setenv("AIMARKET_HUB_NAME", "Independent AI Hub")
    monkeypatch.setenv("AIMARKET_HUB_URL", "https://independentai.network/hub")

    from aimarket_hub.api import create_app

    html = TestClient(create_app()).get("/").text

    assert "<title>Independent AI Hub — AI Economy Protocol</title>" in html
    assert '<h1 id="hero-brand"><span class="grad">Independent AI Hub</span></h1>' in html, (
        "the hero still carries the reference wordmark"
    )
    assert 'window.__HUB_BRAND = "Independent AI Hub";' in html, (
        "the client-side title would re-brand the page back from the dictionary"
    )
    assert '<span class="grad">model</span>market' not in html, "split wordmark survived"


def test_the_reference_deployment_is_untouched(tmp_path, monkeypatch):
    """modelmarket.dev must keep its own wordmark exactly as designed."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AIMARKET_DB_PATH", str(tmp_path / "ref.db"))
    monkeypatch.setenv("AIMARKET_HUB_NAME", "modelmarket.dev")
    monkeypatch.setenv("AIMARKET_HUB_URL", "https://modelmarket.dev")

    from aimarket_hub.api import create_app

    html = TestClient(create_app()).get("/").text
    assert '<span class="grad">model</span>market<span class="grad">.dev</span>' in html
    assert 'window.__HUB_BRAND = "modelmarket.dev";' in html
