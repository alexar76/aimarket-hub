"""Read untrusted HTTP bodies without unbounded automatic decompression."""
from __future__ import annotations

import zlib


class ResponseLimitError(ValueError):
    """The wire body or decoded body exceeded the caller's budget."""


async def read_capped_response(response, *, max_bytes: int) -> bytes:
    """Consume a streaming HTTPX response with separate wire and decoded limits.

    ``aiter_bytes`` expands an entire compressed chunk before yielding it. Its
    caller's counter therefore runs too late for a compression bomb. Decode raw
    chunks with zlib's output limit, before allocating the expanded body.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("response limit must be a positive integer")
    if response.is_stream_consumed:
        # Mock transports may hand back an already-buffered response. Production
        # callers send stream=True; a buffered response cannot be made unbuffered.
        body = response.content
        if len(body) > max_bytes:
            raise ResponseLimitError("response body exceeds limit")
        return body

    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in ("", "identity", "gzip", "deflate"):
        raise ValueError("Unsupported Content-Encoding for bounded response")
    decoder = None if encoding in ("", "identity") else zlib.decompressobj(
        16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
    body = bytearray()
    wire_bytes = 0
    async for chunk in response.aiter_raw(chunk_size=16384):
        wire_bytes += len(chunk)
        if wire_bytes > max_bytes:
            raise ResponseLimitError("response wire body exceeds limit")
        try:
            decoded = decoder.decompress(chunk, max_bytes - len(body) + 1) if decoder else chunk
        except zlib.error as exc:
            raise ValueError("Invalid compressed response") from exc
        body.extend(decoded)
        if len(body) > max_bytes:
            raise ResponseLimitError("response body exceeds limit")
        if decoder and (decoder.unused_data or decoder.unconsumed_tail):
            raise ValueError("Unexpected trailing compressed response data")
    if decoder and not decoder.eof:
        raise ValueError("Truncated compressed response")
    return bytes(body)
