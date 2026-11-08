"""Untrusted schemas cannot bypass HTTP guards through implicit ref retrieval."""
import urllib.request

import pytest

from aimarket_hub.federation_assay import analyze_sandbox_output
from aimarket_hub import validator


@pytest.fixture
def no_retrieval(monkeypatch):
    calls = []

    def forbidden(request, *args, **kwargs):
        calls.append(getattr(request, 'full_url', str(request)))
        raise AssertionError('JSON Schema attempted remote or file retrieval')

    monkeypatch.setattr(urllib.request, 'urlopen', forbidden)
    return calls


@pytest.mark.parametrize('reference', [
    'http://127.0.0.1:8199/schema-canary',
    'https://169.254.169.254/latest/meta-data/',
    'http://private-schema.example/schema',
    'file:///isolated-nonexistent-schema-canary',
    'relative-schema-canary.json',
])
def test_assay_refuses_remote_refs_without_fetching(reference, no_retrieval):
    schema = {'type': 'object', '$ref': reference}
    okay, reason = analyze_sandbox_output(
        {'success': True, 'result': {}}, {'output_schema': schema})
    assert not okay and 'output_schema' in reason
    assert no_retrieval == []


def test_assay_validates_embedded_definitions_offline(no_retrieval):
    schema = {'type': 'object', '$defs': {'number': {'type': 'integer'}},
              'properties': {'answer': {'$ref': '#/$defs/number'}},
              'required': ['answer']}
    good, _ = analyze_sandbox_output({'result': {'answer': 42}}, {'output_schema': schema})
    bad, _ = analyze_sandbox_output({'result': {'answer': 'wrong'}}, {'output_schema': schema})
    assert good and not bad and no_retrieval == []


@pytest.mark.parametrize('keyword', ['$ref', '$dynamicRef'])
def test_protocol_validator_is_offline_even_for_configured_schema(keyword, no_retrieval):
    schema = {'$schema': 'https://json-schema.org/draft/2020-12/schema',
              'type': 'object', keyword: 'http://127.0.0.1:8199/schema-canary'}
    assert validator._validate(schema, {}) == ['schema reference is not embedded']
    assert no_retrieval == []


def test_protocol_local_refs_keep_validation(no_retrieval):
    schema = {'type': 'object', '$defs': {'number': {'type': 'integer'}},
              'properties': {'n': {'$ref': '#/$defs/number'}}}
    assert validator._validate(schema, {'n': 1}) == []
    assert validator._validate(schema, {'n': 'wrong'})
    assert no_retrieval == []
