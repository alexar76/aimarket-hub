"""Seller/provider operation status reads cap bytes before HTTPX decompression."""
from __future__ import annotations

import asyncio
import gzip
import json
import zlib
from urllib.parse import urlsplit

import httpx
import pytest

from aimarket_hub import outbound_http, peer_operations, provider_recovery, response_limits


class RecordedStream(httpx.AsyncByteStream):
    def __init__(self, body, state, *, stall=False):
        self.body, self.state, self.stall = body, state, stall

    async def __aiter__(self):
        if self.stall:
            await asyncio.sleep(1)
        self.state['raw_stream_reads'] += 1
        yield self.body

    async def aclose(self):
        self.state['closed'] = True


def operation_transport(monkeypatch, kind, body, *, encoding=None, status=200, stall=False,
                        provider_url='https://provider.test/invoke', provider_private=False):
    state = {'requests': [], 'raw_stream_reads': 0, 'closed': False}
    real_client = httpx.AsyncClient

    async def prepare(url, *, invoke=False):
        state['prepared'] = {'url': url, 'invoke': invoke}
        return url, {'Host': urlsplit(url).netloc}, {}

    async def handler(request):
        state['requests'].append(request)
        headers = {'Content-Type': 'application/json'}
        if encoding:
            headers['Content-Encoding'] = encoding
        return httpx.Response(status, headers=headers, stream=RecordedStream(body, state, stall=stall))

    def client(**kwargs):
        state['client_options'] = kwargs
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    async def forbidden_automatic_decode(*args, **kwargs):
        raise AssertionError('operation response used HTTPX automatic decompression')
        yield b''  # An async iterator, matching Response.aiter_bytes.

    monkeypatch.setattr(peer_operations, 'prepare_safe_request_async', prepare)
    monkeypatch.setattr(outbound_http, 'prepare_safe_request_async', prepare)
    monkeypatch.setattr(httpx, 'AsyncClient', client)
    monkeypatch.setattr(httpx.Response, 'aiter_bytes', forbidden_automatic_decode)
    monkeypatch.setenv('AIMARKET_INVOKE_HOST_GATEWAY', 'host.docker.internal')
    monkeypatch.setenv('AIMARKET_CAPABILITY_TOKEN', 'internal-test-secret')

    async def call():
        if kind == 'peer':
            return await peer_operations.request_json(
                'POST', 'https://seller.test/ai-market/v2/operations/op_test',
                body={'input': {'safe': 1}}, token='scoped-seller-token')
        return await provider_recovery.request_status({'invoke_url': provider_url, 'allow_private': provider_private}, 'op_test')

    return state, call


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['peer', 'provider'])
@pytest.mark.parametrize('encoding', ['gzip', 'deflate', None])
async def test_normal_json_preserves_operation_headers_and_result(monkeypatch, kind, encoding):
    value = {'status': 'completed', 'result': {'reading': 42}, 'signature': 'opaque-signature'}
    raw = json.dumps(value).encode()
    body = gzip.compress(raw) if encoding == 'gzip' else zlib.compress(raw) if encoding == 'deflate' else raw
    state, call = operation_transport(monkeypatch, kind, body, encoding=encoding)
    assert await call() == value
    assert state['raw_stream_reads'] == 1 and state['closed']
    request = state['requests'][0]
    assert request.headers['Accept-Encoding'] == 'gzip, deflate'
    assert 'Authorization' not in request.headers and 'X-API-Key' not in request.headers
    assert 'X-AIMarket-Internal-Token' not in request.headers
    assert state['client_options']['follow_redirects'] is False
    assert state['client_options']['timeout'] == (30 if kind == 'peer' else 15)
    if kind == 'peer':
        assert request.headers['X-Operation-Token'] == 'scoped-seller-token'
        assert request.method == 'POST' and json.loads(request.content) == {'input': {'safe': 1}}
        assert state['prepared']['invoke'] is False
    else:
        assert 'X-Operation-Token' not in request.headers
        assert request.method == 'GET' and request.url.path == '/invoke/operations/op_test'
        assert state['prepared']['invoke'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['peer', 'provider'])
@pytest.mark.parametrize('encoding', ['gzip', 'deflate'])
async def test_compression_bomb_is_refused_with_bounded_decoder(monkeypatch, kind, encoding):
    raw = b'{"result":"' + b'x' * (8 << 20) + b'"}'
    body = gzip.compress(raw) if encoding == 'gzip' else zlib.compress(raw)
    assert len(body) < 16_384
    state, call = operation_transport(monkeypatch, kind, body, encoding=encoding)
    original = response_limits.zlib.decompressobj
    limits = []

    class BoundedDecoder:
        def __init__(self, decoder):
            self.decoder = decoder

        def __getattr__(self, name):
            return getattr(self.decoder, name)

        def decompress(self, chunk, max_length=0):
            assert 0 < max_length <= 2_000_001, 'unbounded zlib output allocation'
            limits.append(max_length)
            return self.decoder.decompress(chunk, max_length)

    monkeypatch.setattr(response_limits.zlib, 'decompressobj', lambda *a, **k: BoundedDecoder(original(*a, **k)))
    message = 'peer operation response exceeds 2 MB' if kind == 'peer' else 'provider operation exceeds response limit'
    with pytest.raises(ValueError, match=message):
        await call()
    assert limits and state['closed'] and len(state['requests']) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['peer', 'provider'])
async def test_status_error_is_not_decoded_or_retried(monkeypatch, kind):
    state, call = operation_transport(monkeypatch, kind, b'not JSON', status=401)
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await call()
    assert exc.value.response.status_code == 401
    assert state['raw_stream_reads'] == 0 and state['closed'] and len(state['requests']) == 1


@pytest.mark.asyncio
async def test_internal_provider_token_remains_limited_to_operator_gateway(monkeypatch):
    state, call = operation_transport(monkeypatch, 'provider', b'{"status":"running"}',
                                      provider_url='http://host.docker.internal:8080/invoke', provider_private=True)
    assert await call() == {'status': 'running'}
    assert state['requests'][0].headers['X-AIMarket-Internal-Token'] == 'internal-test-secret'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['peer', 'provider'])
async def test_deadline_stays_an_http_timeout_for_reconciliation(monkeypatch, kind):
    state, call = operation_transport(monkeypatch, kind, b'{}', stall=True)
    real_timeout = asyncio.timeout
    deadlines = []

    def short_timeout(seconds):
        deadlines.append(seconds)
        return real_timeout(0.01)

    monkeypatch.setattr(asyncio, 'timeout', short_timeout)
    with pytest.raises(httpx.ReadTimeout):
        await call()
    assert deadlines == [30 if kind == 'peer' else 15]
    assert state['closed'] and len(state['requests']) == 1


@pytest.mark.asyncio
async def test_peer_still_rejects_nonobject_json(monkeypatch):
    _state, call = operation_transport(monkeypatch, 'peer', gzip.compress(b'[]'), encoding='gzip')
    with pytest.raises(ValueError, match='must be an object'):
        await call()
