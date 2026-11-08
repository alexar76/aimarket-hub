"""Signed callback bytes retain their identity through the capped transport."""
import hashlib
import hmac

import httpx
import pytest

from aimarket_hub import outbound_http


@pytest.mark.asyncio
async def test_signed_callback_bytes_are_not_reserialized(monkeypatch):
    body = b'{ "event": "approval", "data": [1, 2] }\n'
    signature = hmac.new(b'fixture-secret', body, hashlib.sha256).hexdigest()
    real_client = httpx.AsyncClient

    async def prepare(url, **kwargs):
        assert not kwargs.get('invoke')
        return 'https://93.184.216.34/callback', {'Host': 'callback.example'}, {}

    def handle(request):
        assert request.content == body
        assert request.headers['x-signature'] == signature
        assert request.headers['host'] == 'callback.example'
        assert request.url.host == '93.184.216.34'
        return httpx.Response(202, stream=httpx.ByteStream(b'accepted'))

    def client(**kwargs):
        return real_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(outbound_http, 'prepare_safe_request_async', prepare)
    monkeypatch.setattr(outbound_http.httpx, 'AsyncClient', client)
    status, reply, _headers = await outbound_http.safe_request_capped(
        'POST', 'https://callback.example/callback', content=body,
        headers={'X-Signature': signature}, max_bytes=4096)
    assert (status, reply) == (202, b'accepted')


@pytest.mark.asyncio
async def test_conflicting_body_formats_fail_before_dns(monkeypatch):
    async def no_dns(*args, **kwargs):
        pytest.fail('invalid request body reached DNS')

    monkeypatch.setattr(outbound_http, 'prepare_safe_request_async', no_dns)
    with pytest.raises(ValueError, match='mutually exclusive'):
        await outbound_http.safe_request_capped(
            'POST', 'https://callback.example', json={}, content=b'{}', max_bytes=4096)
