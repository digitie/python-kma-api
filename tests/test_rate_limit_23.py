"""초당 제한 envelope의 단일 재시도 예산과 정상 응답 해석 비용을 검증한다."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from kma import AsyncTokenBucket, DataGoKrClient, KmaClient, _http
from kma.exceptions import KmaAuthError, KmaRequestError


class CountingBucket(AsyncTokenBucket):
    def __init__(self):
        super().__init__(10000)
        self.count = 0

    async def acquire(self):
        await super().acquire()
        self.count += 1


def envelope(code="00", *, message="NORMAL_SERVICE"):
    return {
        "response": {
            "header": {"resultCode": code, "resultMsg": message},
            "body": {
                "pageNo": 1,
                "numOfRows": 1000,
                "totalCount": 1,
                "items": {"item": [{"category": "T1H", "obsrValue": "23"}]},
            },
        }
    }


def error_response(code, format_, **kwargs):
    if format_ == "json":
        return httpx.Response(200, json=envelope(code, message="serviceKey=canary-key"), **kwargs)
    return httpx.Response(
        200,
        content=(
            '\ufeff <?xml version="1.0" encoding="UTF-8"?>'
            '<g:OpenAPI_ServiceResponse xmlns:g="urn:gateway"><g:cmmMsgHeader>'
            "<g:returnAuthMsg>serviceKey=canary-key</g:returnAuthMsg>"
            f"<g:returnReasonCode>{code}</g:returnReasonCode>"
            "</g:cmmMsgHeader></g:OpenAPI_ServiceResponse>"
        ).encode(),
        **kwargs,
    )


async def call_client(kind, session, bucket, retries):
    cls = KmaClient if kind == "typed" else DataGoKrClient
    async with cls("canary-key", session=session, rate_limiter=bucket, retries=retries) as client:
        if kind == "typed":
            return await client.now(nx=60, ny=127)
        return await client.request("S", "O")


@pytest.mark.parametrize("kind", ["typed", "generic"])
@pytest.mark.parametrize("format_", ["json", "xml"])
async def test_temporary_23_recovers_in_same_http_budget(kind, format_, monkeypatch):
    bucket, sent, sleeps, closed = CountingBucket(), [], [], []

    async def no_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(_http.asyncio, "sleep", no_sleep)

    def handler(request):
        sent.append(bucket.count)
        if len(sent) == 1:
            response = error_response("23", format_, headers={"Retry-After": "2"})
            original_close = response.aclose

            async def record_close():
                closed.append(True)
                await original_close()

            response.aclose = record_close
            return response
        return httpx.Response(200, json=envelope())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as session:
        result = await call_client(kind, session, bucket, retries=2)
    assert sent == [1, 2]
    assert sleeps == [2.0]
    assert closed == [True]
    if kind == "typed":
        assert result.temperature == 23
    else:
        assert result["items"]["item"][0]["obsrValue"] == "23"


@pytest.mark.parametrize("kind", ["typed", "generic"])
@pytest.mark.parametrize("format_", ["json", "xml"])
@pytest.mark.parametrize("retries", [0, 2])
async def test_exhausted_23_is_typed_redacted_and_bounded(kind, format_, retries, monkeypatch):
    bucket, sent, sleeps = CountingBucket(), [], []

    async def no_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(_http.asyncio, "sleep", no_sleep)

    def handler(request):
        sent.append(bucket.count)
        return error_response("23", format_)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as session:
        with pytest.raises(KmaRequestError) as captured:
            await call_client(kind, session, bucket, retries)
    error = captured.value
    assert error.result_code == "23"
    assert error.failure_kind == "rate_limit"
    assert error.retryable is True
    assert "canary-key" not in str(error)
    assert error.__cause__ is None
    assert sent == list(range(1, retries + 2))
    assert len(sleeps) == retries
    assert all(0.15 * 2**i <= delay <= 0.3 * 2**i for i, delay in enumerate(sleeps))


@pytest.mark.parametrize("kind", ["typed", "generic"])
@pytest.mark.parametrize("format_", ["json", "xml"])
@pytest.mark.parametrize("code,failure_kind", [("22", "quota"), ("20", "auth"), ("12", "request")])
async def test_non_retryable_envelopes_keep_single_attempt(kind, format_, code, failure_kind):
    bucket = CountingBucket()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: error_response(code, format_))
    ) as session:
        with pytest.raises((KmaRequestError, KmaAuthError)) as captured:
            await call_client(kind, session, bucket, retries=3)
    assert captured.value.result_code == code
    assert captured.value.failure_kind == failure_kind
    assert captured.value.retryable is False
    assert bucket.count == 1


@pytest.mark.parametrize("kind", ["typed", "generic"])
@pytest.mark.parametrize("format_", ["json", "xml"])
async def test_no_data_03_stays_empty_success(kind, format_):
    bucket = CountingBucket()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: error_response("03", format_))
    ) as session:
        result = await call_client(kind, session, bucket, retries=3)
    assert bucket.count == 1
    if kind == "typed":
        assert result.temperature is None
    else:
        assert result["items"]["item"] == []


async def test_status_and_envelope_share_total_attempt_budget(monkeypatch):
    bucket, sleeps = CountingBucket(), []

    async def no_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(_http.asyncio, "sleep", no_sleep)

    def handler(request):
        if bucket.count == 1:
            return httpx.Response(503)
        return error_response("23", "json")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as session:
        with pytest.raises(KmaRequestError) as captured:
            await call_client("typed", session, bucket, retries=2)
    assert captured.value.result_code == "23"
    assert bucket.count == 3
    assert len(sleeps) == 2


@pytest.mark.parametrize("kind", ["typed", "generic"])
async def test_normal_weather_value_23_has_one_json_decode(kind, monkeypatch):
    original = httpx.Response.json
    decodes = []

    def counted_json(self, **kwargs):
        decodes.append(True)
        return original(self, **kwargs)

    monkeypatch.setattr(httpx.Response, "json", counted_json)
    bucket = CountingBucket()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=envelope()))
    ) as session:
        await call_client(kind, session, bucket, retries=3)
    assert bucket.count == 1
    assert len(decodes) == 1


@pytest.mark.parametrize(
    "content",
    [
        b'{"response":{"header":{"resultCode":"00"},"body":{"obsrValue":"23"}}}',
        b'{"resultCode":"23"}',
        b'{"response":{"header":null},"item":{"resultCode":"23"}}',
        b'{"response":{"header":{"resultCode":"00"}},"item":{"resultCode":"23"}}',
        b'{"response":{"header":{"resultCode":"23"}',
        b"<Other><cmmMsgHeader><returnReasonCode>23</returnReasonCode></cmmMsgHeader></Other>",
        b"<OpenAPI_ServiceResponse><item><returnReasonCode>23</returnReasonCode></item></OpenAPI_ServiceResponse>",
        b"<OpenAPI_ServiceResponse><cmmMsgHeader><returnReasonCode>23</cmmMsgHeader>",
    ],
)
def test_only_standard_error_root_and_header_trigger_retry(content):
    assert _http._is_per_second_limit_envelope(httpx.Response(200, content=content)) is False


@pytest.mark.parametrize("large", [False, True])
def test_normal_and_large_bodies_skip_additional_parsers(large, monkeypatch):
    payload = envelope()
    if large:
        payload["response"]["body"]["padding"] = "x" * 32768
        payload["response"]["body"]["extra"] = {"resultCode": "23"}
    response = httpx.Response(200, json=payload)

    def unexpected_parse(*args, **kwargs):
        raise AssertionError("정상 본문을 추가 해석하면 안 됨")

    monkeypatch.setattr(response, "json", unexpected_parse)
    monkeypatch.setattr(_http.ElementTree, "fromstring", unexpected_parse)
    assert _http._is_per_second_limit_envelope(response) is False


@pytest.mark.parametrize("mutation", ["cancel", "close"])
async def test_retry_wait_preserves_cancellation_and_closed_client(mutation, monkeypatch):
    bucket, sleeps = CountingBucket(), []
    typed = None

    async def stop_during_wait(delay):
        sleeps.append(delay)
        if mutation == "cancel":
            raise asyncio.CancelledError
        await typed.aclose()

    monkeypatch.setattr(_http.asyncio, "sleep", stop_during_wait)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: error_response("23", "json"))
    ) as session:
        typed = KmaClient("canary-key", session=session, rate_limiter=bucket, retries=3)
        expected_error = asyncio.CancelledError if mutation == "cancel" else RuntimeError
        with pytest.raises(expected_error):
            await typed.now(nx=60, ny=127)
        await typed.aclose()
    assert bucket.count == 1
    assert len(sleeps) == 1
