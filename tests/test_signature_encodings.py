"""A key is bytes. The alphabet it was written in is not part of its identity.

The spec uses standard base64 and so does this hub, but two of our own implementations —
the factory's AI-Market signer and the UNI bubble — published unpadded base64url. Python's
`base64.b64decode` does not reject `-`/`_`: with the default `validate=False` it *discards*
them and returns different bytes, so a good signature verified as garbage and the peer was
told its signature did not match its key. Found live on 2026-08-31, on our own factory.
"""

from __future__ import annotations

import base64
import pathlib
import tempfile

import pytest

from aimarket_hub.signing import Signer, decode_b64, same_key


@pytest.fixture
def signer() -> Signer:
    return Signer(str(pathlib.Path(tempfile.mkdtemp()) / "key"))


def _urlsafe(standard_b64: str) -> str:
    return base64.urlsafe_b64encode(base64.b64decode(standard_b64)).decode().rstrip("=")


def test_a_urlsafe_signature_verifies(signer):
    canonical = "capabilities_count:1|generated_at:x|protocol_version:v1"
    value = signer.sign_canonical(canonical)
    assert signer.verify(signer.public_key_b64, value, canonical)
    assert signer.verify(_urlsafe(signer.public_key_b64), _urlsafe(value), canonical), (
        "the same key and signature in base64url must verify, not decode to garbage"
    )


def test_a_wrong_signature_still_fails_in_either_alphabet(signer):
    canonical = "capabilities_count:1|generated_at:x|protocol_version:v1"
    value = signer.sign_canonical(canonical)
    assert not signer.verify(signer.public_key_b64, value, canonical + "!")
    assert not signer.verify(_urlsafe(signer.public_key_b64), _urlsafe(value), canonical + "!")


def test_garbage_is_not_a_signature(signer):
    canonical = "anything"
    assert not signer.verify(signer.public_key_b64, "not base64 at all!!", canonical)
    assert not signer.verify("", signer.sign_canonical(canonical), canonical)


def test_decode_reads_both_alphabets_and_refuses_the_rest():
    raw = bytes(range(32))
    assert decode_b64(base64.b64encode(raw).decode()) == raw
    assert decode_b64(base64.urlsafe_b64encode(raw).decode().rstrip("=")) == raw
    assert decode_b64("") is None
    assert decode_b64("nope nope") is None


def test_the_same_key_in_two_alphabets_is_not_a_rotation(signer):
    assert same_key(signer.public_key_b64, _urlsafe(signer.public_key_b64))
    other = Signer(str(pathlib.Path(tempfile.mkdtemp()) / "key2"))
    assert not same_key(signer.public_key_b64, other.public_key_b64)
    assert not same_key(signer.public_key_b64, "")
