"""An unauthenticated burst cannot start an unbounded number of live assays."""
import asyncio

import httpx
import pytest

from aimarket_hub.assay_limits import MAX_CONCURRENT_ASSAYS, limit_assay_concurrency, schedule_background_assay
from aimarket_hub import assay_limits
from aimarket_hub import federation_assay
from aimarket_hub.api import create_app
from aimarket_hub.config import HubConfig
from aimarket_hub.database import HubDatabase
from aimarket_hub.signing import Signer


@pytest.mark.asyncio
async def test_real_assay_burst_bounds_concurrent_network_work(monkeypatch, tmp_path):
    monkeypatch.setattr(federation_assay._crawler, "_url_is_safe", lambda _url: True)
    release = asyncio.Event()
    reached = asyncio.Event()
    active = peak = 0

    async def controlled_get(_url):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == MAX_CONCURRENT_ASSAYS:
            reached.set()
        try:
            await release.wait()
            return {}  # invalid discovery ends without a sandbox or judge call
        finally:
            active -= 1

    async def forbidden_post(*args, **kwargs):
        pytest.fail("controlled invalid discovery must not invoke a capability")

    config = HubConfig()
    config.federation_auto_admit = False
    db = HubDatabase(tmp_path / "hub.db")
    signer = Signer(tmp_path / "key")
    tasks = [asyncio.create_task(federation_assay.run_assay(
        f"https://controlled-{i}.example", config=config, db=db, signer=signer,
        get_json=controlled_get, post_json=forbidden_post, crawl_on_admit=False,
    )) for i in range(MAX_CONCURRENT_ASSAYS * 3)]
    try:
        await asyncio.wait_for(reached.wait(), timeout=1)
        await asyncio.sleep(0.02)
        assert peak == MAX_CONCURRENT_ASSAYS
        release.set()
        await asyncio.gather(*tasks)
        assert peak == MAX_CONCURRENT_ASSAYS
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        db.close()


@pytest.mark.asyncio
async def test_cancelled_waiter_and_failed_assay_do_not_lose_capacity():
    entered = asyncio.Event()
    release = asyncio.Event()

    @limit_assay_concurrency
    async def work(fail=False):
        entered.set()
        await release.wait()
        if fail:
            raise ValueError("controlled failure")
        return True

    holders = [asyncio.create_task(work(fail=True)) for _ in range(MAX_CONCURRENT_ASSAYS)]
    await entered.wait()
    await asyncio.sleep(0)
    waiter = asyncio.create_task(work())
    await asyncio.sleep(0)
    waiter.cancel()
    release.set()
    await asyncio.gather(*holders, waiter, return_exceptions=True)
    assert await asyncio.wait_for(work(), timeout=1) is True


@pytest.mark.asyncio
async def test_duplicate_background_burst_creates_one_task_and_one_coroutine():
    release = asyncio.Event()
    entered = asyncio.Event()
    factories_called = 0

    async def work():
        entered.set()
        await release.wait()

    def factory():
        nonlocal factories_called
        factories_called += 1
        return work()

    loop = asyncio.get_running_loop()
    answers = [schedule_background_assay('https://same.example/', factory) for _ in range(20)]
    assert answers == [True] + [False] * 19
    await entered.wait()
    assert factories_called == 1 and len(assay_limits._background_tasks[loop]) == 1
    tasks = list(assay_limits._background_tasks[loop].values())
    release.set()
    await asyncio.gather(*tasks)
    await asyncio.sleep(0)
    assert loop not in assay_limits._background_tasks


@pytest.mark.asyncio
async def test_unique_background_burst_bounds_task_objects_without_waiters():
    release = asyncio.Event()
    calls = 0

    async def work():
        nonlocal calls
        calls += 1
        await release.wait()

    loop = asyncio.get_running_loop()
    answers = [schedule_background_assay(f'https://peer-{i}.example', work) for i in range(20)]
    assert sum(answers) == MAX_CONCURRENT_ASSAYS
    tasks = list(assay_limits._background_tasks[loop].values())
    assert len(tasks) == MAX_CONCURRENT_ASSAYS
    await asyncio.sleep(0)
    assert calls == MAX_CONCURRENT_ASSAYS
    release.set()
    await asyncio.gather(*tasks)
    await asyncio.sleep(0)
    assert loop not in assay_limits._background_tasks


@pytest.mark.asyncio
async def test_cancel_and_failure_drop_strong_task_refs_and_reuse_capacity():
    entered = asyncio.Event()
    release = asyncio.Event()
    loop = asyncio.get_running_loop()

    async def waiting():
        entered.set()
        await release.wait()

    assert schedule_background_assay('https://cancel.example', waiting)
    await entered.wait()
    task = assay_limits._background_tasks[loop]['https://cancel.example']
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    assert loop not in assay_limits._background_tasks

    async def failed():
        raise ValueError('controlled background failure')

    assert schedule_background_assay('https://cancel.example', failed)
    task = assay_limits._background_tasks[loop]['https://cancel.example']
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    assert loop not in assay_limits._background_tasks
    release.set()
    assert schedule_background_assay('https://cancel.example', waiting)
    await asyncio.gather(*assay_limits._background_tasks[loop].values())
    await asyncio.sleep(0)
    assert loop not in assay_limits._background_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize('duplicate', [True, False])
async def test_real_announce_burst_reports_only_actually_scheduled_work(monkeypatch, tmp_path, duplicate):
    import aimarket_hub.crawler as crawler_module
    monkeypatch.setenv('AIMARKET_ADMIN_TOKEN', 'controlled-admin-token')
    monkeypatch.setenv('AIMARKET_CHAIN_REALM', 'live')
    monkeypatch.setenv('AIMARKET_FEDERATION_ASSAY', '1')
    monkeypatch.setenv('AIMARKET_HUB_URL', 'https://controlled-hub.example')
    monkeypatch.setattr(crawler_module, '_url_is_safe', lambda _url: True)
    release = asyncio.Event()
    calls = []

    async def controlled_assay(url, **kwargs):
        calls.append(url)
        await release.wait()
        return {'url': url, 'verdict': 'review'}

    monkeypatch.setattr(federation_assay, 'run_assay', controlled_assay)
    config = HubConfig()
    config.db_path = str(tmp_path / 'hub.db')
    config.federation_assay = True
    config.seed_list = []
    db = HubDatabase(config.db_path)
    app = create_app(config=config, db=db, signer=Signer(tmp_path / 'key'))
    loop = asyncio.get_running_loop()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=config.hub_url) as client:
            responses = await asyncio.gather(*[
                client.post('/ai-market/v2/federation/announce', json={
                    'hub_url': 'https://same.example' if duplicate else f'https://peer-{i}.example',
                }) for i in range(20)
            ])
        assert all(response.status_code == 200 for response in responses)
        expected = 1 if duplicate else MAX_CONCURRENT_ASSAYS
        assert sum(response.json()['assay_scheduled'] for response in responses) == expected
        await asyncio.sleep(0)
        assert len(calls) == expected
        assert len(assay_limits._background_tasks[loop]) == expected
        # Busy knocks remain recorded for a later ordinary crawler cycle.
        assert len(db.list_pending_peers()) == (1 if duplicate else 20)
    finally:
        tasks = list(assay_limits._background_tasks.get(loop, {}).values())
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)
        db.close()
    assert loop not in assay_limits._background_tasks
