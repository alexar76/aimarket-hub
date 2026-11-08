"""The capped stranger transport has a whole-request deadline."""
import asyncio
import time

import httpx
import pytest

from aimarket_hub import outbound_http


@pytest.mark.asyncio
async def test_capped_deadline_includes_dns(monkeypatch):
    async def slow_dns(*args, **kwargs):
        await asyncio.sleep(0.3)
        pytest.fail('request continued after deadline')

    monkeypatch.setattr(outbound_http, 'prepare_safe_request_async', slow_dns)
    started = time.monotonic()
    with pytest.raises(httpx.ReadTimeout, match='deadline exceeded'):
        await outbound_http.safe_request_capped('POST', 'https://peer.example/invoke',
                                                timeout=0.03, max_bytes=100)
    assert time.monotonic() - started < 0.2


@pytest.mark.asyncio
async def test_trickle_cannot_hold_a_capped_assay_forever(monkeypatch):
    class Trickle(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            for _ in range(100):
                await asyncio.sleep(0.01)
                yield b'x'

        async def aclose(self):
            self.closed = True

    stream = Trickle()
    real_client = httpx.AsyncClient

    async def ready(*args, **kwargs):
        return 'https://93.184.216.34/invoke', {'Host': 'peer.example'}, {}

    def client(**kwargs):
        return real_client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=stream)), **kwargs)

    monkeypatch.setattr(outbound_http, 'prepare_safe_request_async', ready)
    monkeypatch.setattr(outbound_http.httpx, 'AsyncClient', client)
    started = time.monotonic()
    with pytest.raises(httpx.ReadTimeout, match='deadline exceeded'):
        await outbound_http.safe_request_capped('POST', 'https://peer.example/invoke',
                                                timeout=0.04, max_bytes=100)
    assert time.monotonic() - started < 0.3
    assert stream.closed
