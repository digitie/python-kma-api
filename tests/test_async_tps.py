"""공통 예산을 쓰는 실제 HTTPX 송신 경로와 비동기 공개 계약 회귀 검증."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
from urllib.parse import quote

import httpx
import pytest

import kma
from kma import ApiHubClient, ApiHubGeneratedClient, AsyncTokenBucket, DataGoKrClient, KmaClient
from kma.apihub_endpoints import APIHUB_ATTACHMENTS, APIHUB_ENDPOINTS
from kma.exceptions import KmaAuthError, KmaError
from tools import update_apihub_endpoints as generator


class CountingBucket(AsyncTokenBucket):
    def __init__(self):
        super().__init__(10000)
        self.count = 0

    async def acquire(self):
        await super().acquire()
        self.count += 1


def payload(*, page=1, total=1, items=None):
    return {
        "response": {
            "header": {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"},
            "body": {
                "pageNo": page,
                "numOfRows": 1,
                "totalCount": total,
                "items": {"item": [] if items is None else items},
            },
        }
    }


async def test_shared_budget_covers_clients_debug_pagination_retry_and_redirect(monkeypatch):
    budget = CountingBucket()
    sent = []

    async def no_sleep(_delay):
        pass

    monkeypatch.setattr(kma._http.asyncio, "sleep", no_sleep)

    def handler(request):
        sent.append((str(request.url), budget.count))
        if len(sent) == 1:
            return httpx.Response(503)
        if len(sent) == 2:
            return httpx.Response(302, headers={"Location": "/resolved"})
        page = int(request.url.params.get("pageNo", 1))
        total = 2 if request.url.path.endswith("/pages") else 1
        return httpx.Response(200, json=payload(page=page, total=total))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as session:
        async with KmaClient("test-key", session=session, rate_limiter=budget) as typed:
            await typed.now(nx=60, ny=127)
        async with DataGoKrClient("test-key", session=session, rate_limiter=budget) as generic:
            await generic.debug_fetch("S", "O")
            pages = [page async for page in generic.iter_pages("S", "pages", num_of_rows=1)]
            assert len(pages) == 2
        async with ApiHubClient("test-key", session=session, rate_limiter=budget) as hub:
            await hub.request_path("/api/test")
        async with ApiHubGeneratedClient(
            "test-key", session=session, rate_limiter=budget
        ) as generated:
            await generated.call_endpoint(APIHUB_ENDPOINTS[0].name, use_sample=True)
            await generated.debug_fetch_endpoint(APIHUB_ENDPOINTS[0], use_sample=True)
        assert not session.is_closed
    assert [count for _, count in sent] == list(range(1, 10))
    assert budget.count == 9


@pytest.mark.parametrize("mutation", ["close", "auth", "cancel"])
async def test_redirect_wait_rechecks_close_and_auth_and_honors_cancellation(mutation):
    entered, release = asyncio.Event(), asyncio.Event()

    class WaitingBucket(CountingBucket):
        async def acquire(self):
            if self.count == 1:
                entered.set()
                await release.wait()
            await super().acquire()

    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(302, headers={"Location": "/next"})

    budget = WaitingBucket()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as session:
        client = ApiHubClient("test-key", session=session, rate_limiter=budget)
        task = asyncio.create_task(client.request_path("/api/start"))
        await asyncio.wait_for(entered.wait(), 2)
        if mutation == "close":
            await client.aclose()
            error = RuntimeError
        elif mutation == "auth":
            session.auth = httpx.DigestAuth("user", "pass")
            error = TypeError
        else:
            task.cancel()
            error = asyncio.CancelledError
        release.set()
        with pytest.raises(error):
            await task
        await client.aclose()
        assert not session.is_closed
    assert len(sent) == 1
    if mutation == "cancel":
        assert budget.count == 1


@pytest.mark.parametrize(
    "client_type", [KmaClient, DataGoKrClient, ApiHubClient, ApiHubGeneratedClient]
)
async def test_owned_lifecycle_sync_rejection_and_public_contract(client_type):
    with httpx.Client() as sync:
        with pytest.raises(TypeError, match="async"):
            client_type("test-key", session=sync)
    async with httpx.AsyncClient(auth=httpx.DigestAuth("u", "p")) as digest:
        with pytest.raises(TypeError, match="unmetered"):
            client_type("test-key", session=digest)
    client = client_type("test-key")
    assert client._session is None
    assert client.rate_limiter.max_rps == 5
    session = client.session
    assert isinstance(session, httpx.AsyncClient)
    await client.aclose()
    await client.aclose()
    assert session.is_closed
    with pytest.raises(RuntimeError, match="closed"):
        async with client:
            pass
    assert not hasattr(client, "aio") and not hasattr(client, "close")


@pytest.mark.parametrize("status", [401, 403, 429, 503])
async def test_status_retries_consume_one_token_per_attempt(status, monkeypatch):
    budget = CountingBucket()

    async def no_sleep(_delay):
        pass

    monkeypatch.setattr(kma._http.asyncio, "sleep", no_sleep)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status))
    ) as session:
        client = DataGoKrClient("test-key", session=session, rate_limiter=budget, retries=2)
        with pytest.raises(KmaError) as error:
            await client.request("S", "O")
        assert error.value.status_code == status
    assert budget.count == (1 if status in {401, 403} else 3)


async def test_public_errors_and_entire_debug_results_mask_actual_and_overridden_keys():
    secret, overridden = "key-secret/+abcdef", "override-secret/+uvwxyz"
    echo = f"{secret} {quote(secret, safe='')} {overridden}"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"response": {"header": {"resultCode": "30", "resultMsg": echo}}}
            )
        )
    ) as session:
        client = DataGoKrClient(secret, session=session)
        with pytest.raises(KmaAuthError) as captured:
            await client.request("S", "O", {"serviceKey": overridden})
        assert secret not in repr(captured.value.args)
        assert overridden not in repr(captured.value.metadata)
        run = await client.debug_fetch("S", "O", {"serviceKey": overridden})
        assert secret not in repr(dataclasses.asdict(run))
        assert overridden not in repr(dataclasses.asdict(run))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=echo))
    ) as session:
        client = ApiHubGeneratedClient(secret, session=session)
        run = await client.debug_fetch_endpoint(APIHUB_ENDPOINTS[0], use_sample=True)
        assert secret not in repr(dataclasses.asdict(run))


async def test_generator_reproduces_all_470_async_endpoint_calls():
    endpoint_fields = {field.name for field in dataclasses.fields(generator.Endpoint)} - {
        "examples"
    }
    attachment_fields = {field.name for field in dataclasses.fields(generator.Attachment)}
    endpoints = [
        generator.Endpoint(**{key: getattr(spec, key) for key in endpoint_fields})
        for spec in APIHUB_ENDPOINTS
    ]
    attachments = [
        generator.Attachment(**{key: getattr(spec, key) for key in attachment_fields})
        for spec in APIHUB_ATTACHMENTS
    ]
    code = generator.render_module(endpoints, attachments)
    namespace = {"__name__": "kma._generated_test", "__package__": "kma"}
    exec(compile(code, "<generated>", "exec"), namespace)
    generated_type = namespace["ApiHubGeneratedClient"]
    budget = CountingBucket()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="col\nvalue"))
    ) as session:
        client = generated_type("test-key", session=session, rate_limiter=budget)
        for spec in APIHUB_ENDPOINTS:
            assert inspect.iscoroutinefunction(getattr(ApiHubGeneratedClient, spec.name))
            method = getattr(client, spec.name)
            assert inspect.iscoroutinefunction(method)
            params = {key: spec.sample_params.get(key, "0") for key in spec.parameters}
            response = await method(**params)
            assert response.status_code == 200
    assert len(APIHUB_ENDPOINTS) == budget.count == 470


@pytest.mark.parametrize("method", ["forecast_short", "forecast"])
async def test_forecast_parse_errors_protect_echoed_keys(method):
    secret = "synthetic/key+value"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=payload(
                    items=[
                        {
                            "category": "TMP",
                            "fcstValue": secret,
                        }
                    ]
                ),
            )
        )
    ) as session:
        client = KmaClient(secret, session=session)
        with pytest.raises(KmaError) as captured:
            await getattr(client, method)(nx=60, ny=127)
        assert secret not in str(captured.value)
        assert secret not in repr(captured.value.args)


async def test_open_api_error_and_custom_credential_override_are_protected(caplog):
    secret = "overridden/key+value"
    caplog.set_level("INFO", logger="httpx")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "response": {
                        "header": {
                            "resultCode": "30",
                            "resultMsg": secret,
                        }
                    }
                },
            )
        )
    ) as session:
        hub = ApiHubClient(secret, session=session)
        with pytest.raises(KmaAuthError) as captured:
            await hub.open_api("S", "O")
        assert secret not in str(captured.value)
        client = DataGoKrClient("configured-key", session=session, service_key_param="customKey")
        with pytest.raises(KmaAuthError) as captured:
            await client.request("S", "O", {"customKey": secret})
        assert secret not in str(captured.value)
        run = await client.debug_fetch("S", "O", {"customKey": secret})
        assert secret not in str(run)
        assert secret not in caplog.text
        assert quote(secret, safe="") not in caplog.text
