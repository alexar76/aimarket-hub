"""Crawler requests pin DNS and enforce response limits before buffering."""

from __future__ import annotations

import socket
import asyncio
import gzip
import time
import json
from types import SimpleNamespace

import httpx
import pytest

from aimarket_hub import crawler as crawler_module
from aimarket_hub.crawler import Crawler, MAX_RESPONSE_BYTES


def _public_dns(_host, port, *_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def _bare_crawler(client: httpx.AsyncClient) -> Crawler:
    crawler = Crawler.__new__(Crawler)
    crawler.config = SimpleNamespace(hub_url="https://local-hub.example")
    crawler._http = client
    return crawler


@pytest.mark.asyncio
async def test_crawler_connects_to_the_pinned_ip(monkeypatch) -> None:
    monkeypatch.setattr(crawler_module, "_url_is_safe", lambda _url: True)
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "peer.example"
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json={"ok": True},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await _bare_crawler(client)._safe_get("https://peer.example/manifest")

    assert response.json() == {"ok": True}


class _CountingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: int, chunk_size: int):
        self.chunks = chunks
        self.chunk_size = chunk_size
        self.yielded = 0

    async def __aiter__(self):
        for _ in range(self.chunks):
            self.yielded += 1
            yield b"x" * self.chunk_size


@pytest.mark.asyncio
async def test_chunked_response_stops_at_limit_before_full_download(monkeypatch) -> None:
    monkeypatch.setattr(crawler_module, "_url_is_safe", lambda _url: True)
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    stream = _CountingStream(chunks=100, chunk_size=65536)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "application/json"}, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="exceeds 2 MB"):
            await _bare_crawler(client)._safe_get("https://peer.example/manifest")

    assert stream.yielded < stream.chunks
    assert stream.yielded <= (MAX_RESPONSE_BYTES // stream.chunk_size) + 1


class _WireStream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, delay=0):
        self.chunks = chunks
        self.delay = delay
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_compression_bomb_never_uses_unbounded_httpx_decoder(monkeypatch):
    import httpx._decoders
    from aimarket_hub import response_limits

    monkeypatch.setattr(crawler_module, "_url_is_safe", lambda _url: True)
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    raw = b'{"data":"' + b"a" * (MAX_RESPONSE_BYTES * 8) + b'"}'
    wire = gzip.compress(raw)
    stream = _WireStream([wire])
    outputs = []
    real_decoder = response_limits.zlib.decompressobj

    class BoundedDecoder:
        def __init__(self, *args):
            self.inner = real_decoder(*args)

        def decompress(self, data, max_length=0):
            assert 0 < max_length <= MAX_RESPONSE_BYTES + 1
            value = self.inner.decompress(data, max_length)
            outputs.append(len(value))
            return value

        def __getattr__(self, name):
            return getattr(self.inner, name)

    def no_unbounded_decode(*args):
        pytest.fail("HTTPX expanded a compressed chunk before the size check")

    monkeypatch.setattr(response_limits.zlib, "decompressobj", BoundedDecoder)
    monkeypatch.setattr(httpx._decoders.GZipDecoder, "decode", no_unbounded_decode)

    async def handler(_request):
        return httpx.Response(200, headers={"Content-Type": "application/json",
                                           "Content-Encoding": "gzip",
                                           "Content-Length": str(len(wire))}, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="exceeds 2 MB"):
            await _bare_crawler(client)._safe_get("https://peer.example/manifest")
    assert outputs and max(outputs) <= MAX_RESPONSE_BYTES + 1
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("body,encoding,expected", [
    (gzip.compress(b'{"ok":true}'), "gzip", {"ok": True}),
    (b'{"ok":true}', "identity", {"ok": True}),
])
async def test_bounded_reader_keeps_normal_json_working(monkeypatch, body, encoding, expected):
    monkeypatch.setattr(crawler_module, "_url_is_safe", lambda _url: True)
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)

    async def handler(request):
        assert request.headers["accept-encoding"] == "gzip, deflate"
        return httpx.Response(200, headers={"Content-Type": "application/json",
                                           "Content-Encoding": encoding}, stream=_WireStream([body]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert (await _bare_crawler(client)._safe_get("https://peer.example/manifest")).json() == expected


@pytest.mark.asyncio
async def test_truncated_compressed_stream_is_rejected(monkeypatch):
    monkeypatch.setattr(crawler_module, "_url_is_safe", lambda _url: True)
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    wire = gzip.compress(b'{"ok":true}')[:-4]

    async def handler(_request):
        return httpx.Response(200, headers={"Content-Type": "application/json",
                                           "Content-Encoding": "gzip"}, stream=_WireStream([wire]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="Truncated compressed response"):
            await _bare_crawler(client)._safe_get("https://peer.example/manifest")


@pytest.mark.asyncio
async def test_trickled_body_cannot_extend_total_deadline(monkeypatch):
    monkeypatch.setattr(crawler_module, "_url_is_safe", lambda _url: True)
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    stream = _WireStream([b" "] * 100, delay=0.02)

    async def handler(_request):
        return httpx.Response(200, headers={"Content-Type": "application/json"}, stream=stream)

    started = time.monotonic()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        crawler = _bare_crawler(client)
        crawler.config.request_timeout_s = 0.05
        with pytest.raises(TimeoutError):
            await crawler._safe_get("https://peer.example/manifest")
    assert time.monotonic() - started < 0.5
    assert stream.closed


@pytest.mark.asyncio
async def test_operator_rejection_applies_before_dns_even_outside_bfs(monkeypatch, tmp_path):
    policy = tmp_path / "rejections.json"
    policy.write_text(json.dumps({"version": 1, "peer_urls": ["https://rejected.example"],
                                  "invoke_urls": [], "capability_keys": []}))
    monkeypatch.setenv("AIMARKET_PEER_REJECTIONS_FILE", str(policy))

    def no_dns(*args, **kwargs):
        pytest.fail("rejected peer reached DNS")

    async def no_http(_request):
        pytest.fail("rejected peer reached HTTP")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    async with httpx.AsyncClient(transport=httpx.MockTransport(no_http)) as client:
        with pytest.raises(ValueError, match="peer rejected"):
            await _bare_crawler(client)._safe_get("https://rejected.example/.well-known/ai-market.json")
