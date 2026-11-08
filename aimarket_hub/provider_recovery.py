"""Read a cooperative provider's durable, signed result; never redispatch work."""
from __future__ import annotations
import hashlib
import json
import os
from urllib.parse import urlparse
import httpx
from aimarket_hub.signing import Signer

PROTOCOL = 'PROVIDER-OP/1'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def descriptor(cap):
    # Explicit operator opt-in; a random legacy endpoint is never probed for recovery.
    products = {x.strip() for x in os.getenv('AIMARKET_PROVIDER_OPERATION_PRODUCTS', '').split(',') if x.strip()}
    if cap and cap.product_id in products and cap.source_hub == 'local' and cap.invoke_url and cap.provider_pubkey:
        from aimarket_hub.publish import private_invoke_authorized
        return {'protocol': PROTOCOL, 'invoke_url': cap.invoke_url, 'public_key': cap.provider_pubkey,
                'allow_private': private_invoke_authorized(cap)}
    return None


async def request_status(spec, operation_id):
    import asyncio
    from aimarket_hub.outbound_http import (resolve_invoke_url, prepare_safe_request_async,
        invoke_gateway_hosts)
    from aimarket_hub.response_limits import read_capped_response, ResponseLimitError

    try:
        async with asyncio.timeout(15):
            # Only descriptor() can grant this authority, captured with the immutable
            # order. Legacy specs and public request fields never inherit gateway trust.
            allow_private = spec.get('allow_private') is True
            url = spec['invoke_url'].rstrip('/') + '/operations/' + operation_id
            if allow_private:
                url = resolve_invoke_url(url)
            target, headers, extensions = await prepare_safe_request_async(url, invoke=allow_private)
            headers['Accept-Encoding'] = 'gzip, deflate'
            token = os.getenv('AIMARKET_CAPABILITY_TOKEN', '').strip()
            if allow_private and token and (urlparse(url).hostname or '').lower() in set(invoke_gateway_hosts()):
                headers['X-AIMarket-Internal-Token'] = token
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                async with client.stream('GET', target, headers=headers, extensions=extensions or None) as response:
                    response.raise_for_status()
                    try:
                        raw = await read_capped_response(response, max_bytes=2_000_000)
                    except ResponseLimitError as exc:
                        raise ValueError('provider operation exceeds response limit') from exc
    except TimeoutError as exc:
        raise httpx.ReadTimeout('provider operation request deadline exceeded') from exc
    return json.loads(raw)


def verify(document, spec, operation_id, node, payload):
    if not isinstance(document, dict):
        raise ValueError('invalid provider operation')
    unsigned = {k: v for k, v in document.items() if k != 'signature'}
    if not Signer.verify(spec['public_key'], document.get('signature', ''), canonical(unsigned)):
        raise ValueError('invalid provider operation signature')
    expected = {'protocol': PROTOCOL, 'operation_id': operation_id,
                'product_id': node['product_id'], 'capability_id': node['capability_id'],
                'input_sha256': hashlib.sha256(canonical(payload).encode()).hexdigest()}
    if any(document.get(k) != v for k, v in expected.items()):
        raise ValueError('provider operation differs from the immutable order')
    if document.get('status') not in ('running', 'completed', 'reconciliation_required'):
        raise ValueError('unknown provider operation status')
    if document['status'] == 'completed':
        from aimarket_hub.supply_security import _bound_response_canonical
        bound = _bound_response_canonical(node['capability_id'], node['product_id'], payload, document['result'])
        if not Signer.verify(spec['public_key'], document.get('result_signature', ''), bound):
            raise ValueError('invalid recovered provider result signature')
    return document
