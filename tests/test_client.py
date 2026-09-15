from __future__ import annotations

import inspect
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

from kma.client import KmaClient
from kma.enums import WeatherCategory
from kma.exceptions import KmaAuthError, KmaParseError, KmaRequestError, KmaServerError
from kma.locations import GridPoint, LatLon
from kma.time_utils import KST

T = TypeVar("T", bound=BaseException)


class FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class XmlErrorResponse:
    status_code = 200
    text = """<?xml version="1.0" encoding="UTF-8"?>
<OpenAPI_ServiceResponse><cmmMsgHeader>
<errMsg>SERVICE ERROR</errMsg>
<returnAuthMsg>LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR</returnAuthMsg>
<returnReasonCode>22</returnReasonCode>
</cmmMsgHeader></OpenAPI_ServiceResponse>"""

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        raise ValueError("XML body")


class XmlErrorSession:
    async def get(self, url: str, *, params: dict[str, Any], timeout: float) -> XmlErrorResponse:
        del url, params, timeout
        return XmlErrorResponse()


class XmlNoDataResponse(XmlErrorResponse):
    text = """<OpenAPI_ServiceResponse><cmmMsgHeader>
<returnAuthMsg>NO_DATA</returnAuthMsg><returnReasonCode>03</returnReasonCode>
</cmmMsgHeader></OpenAPI_ServiceResponse>"""


class XmlNoDataSession(XmlErrorSession):
    async def get(self, url: str, *, params: dict[str, Any], timeout: float) -> XmlNoDataResponse:
        del url, params, timeout
        return XmlNoDataResponse()


class FakeSession:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    @property
    def last_params(self) -> dict[str, Any] | None:
        if not self.calls:
            return None
        return self.calls[-1]["params"]

    @property
    def last_url(self) -> str | None:
        if not self.calls:
            return None
        return self.calls[-1]["url"]

    async def get(self, url: str, *, params: dict[str, Any], timeout: float) -> FakeResponse:
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return FakeResponse(self.payload)


class AsyncFakeSession:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def get(self, url: str, *, params: dict[str, Any], timeout: float) -> FakeResponse:
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return FakeResponse(self.payload)

    async def aclose(self) -> None:
        self.closed = True


async def assert_raises(exc_type: type[T], func: Callable[[], object]) -> T:
    try:
        result = func()
        if inspect.isawaitable(result):
            await result
    except exc_type as exc:
        return exc
    except Exception as exc:  # pragma: no cover - failure path for clearer direct-run output
        raise AssertionError(f"expected {exc_type.__name__}, got {type(exc).__name__}") from exc
    raise AssertionError(f"expected {exc_type.__name__}")


def _payload(items: Any) -> dict[str, Any]:
    return {
        "response": {
            "header": {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"},
            "body": {"items": {"item": items}},
        }
    }


def _paged_payload(
    items: Any, *, page_no: int, num_of_rows: int, total_count: int
) -> dict[str, Any]:
    return {
        "response": {
            "header": {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"},
            "body": {
                "pageNo": page_no,
                "numOfRows": num_of_rows,
                "totalCount": total_count,
                "items": {"item": items},
            },
        }
    }


class PagedFakeSession:
    """Answers with a different payload per ``pageNo``, keyed by the request."""

    def __init__(self, payloads_by_page: dict[int, dict[str, Any]]) -> None:
        self.payloads_by_page = payloads_by_page
        self.calls: list[dict[str, Any]] = []

    async def get(self, url: str, *, params: dict[str, Any], timeout: float) -> FakeResponse:
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        page_no = int(params["pageNo"])
        return FakeResponse(self.payloads_by_page[page_no])

    @property
    def requested_pages(self) -> list[int]:
        return [int(call["params"]["pageNo"]) for call in self.calls]


def _error_payload(code: str, message: str = "ERROR") -> dict[str, Any]:
    return {
        "response": {
            "header": {"resultCode": code, "resultMsg": message},
            "body": {},
        }
    }


async def test_now_pivots_observed_items() -> None:
    session = FakeSession(
        _payload(
            [
                {"category": "T1H", "obsrValue": "18.4"},
                {"category": "REH", "obsrValue": "52"},
                {"category": "WSD", "obsrValue": "3.1"},
                {"category": "VEC", "obsrValue": "270"},
                {"category": "RN1", "obsrValue": "강수없음"},
                {"category": "PTY", "obsrValue": "0"},
            ]
        )
    )
    client = KmaClient("decoded-key", session=session)

    snapshot = await client.now(nx=60, ny=127, when=datetime(2026, 4, 30, 14, 45, tzinfo=KST))

    assert snapshot.observed_at.isoformat() == "2026-04-30T14:00:00+09:00"
    assert snapshot.temperature == 18.4
    assert snapshot.humidity == 52
    assert snapshot.wind_speed == 3.1
    assert snapshot.wind_direction == 270
    assert snapshot.precipitation == 0.0
    assert snapshot.precipitation_label == "없음"
    assert snapshot.metadata is not None
    assert snapshot.metadata.provider == "data.go.kr"
    assert snapshot.metadata.service_name == "VilageFcstInfoService_2.0"
    assert snapshot.metadata.endpoint == "getUltraSrtNcst"
    assert snapshot.metadata.base_date == "20260430"
    assert snapshot.metadata.base_time == "1400"
    assert "serviceKey" not in snapshot.metadata.request_params
    assert session.last_params is not None
    assert session.last_params["serviceKey"] == "decoded-key"
    assert session.last_params["base_date"] == "20260430"
    assert session.last_params["base_time"] == "1400"
    assert session.last_params["dataType"] == "JSON"


async def test_forecast_service_matches_krheritage_style_facade() -> None:
    session = FakeSession(
        _payload(
            {
                "baseDate": "20260430",
                "baseTime": "1400",
                "fcstDate": "20260430",
                "fcstTime": "1500",
                "nx": "60",
                "ny": "127",
                "category": "TMP",
                "fcstValue": "18.4",
            }
        )
    )
    client = KmaClient("decoded-key", session=session)

    items = await client.forecast.vilage(
        nx=60,
        ny=127,
        when=datetime(2026, 4, 30, 14, 15, tzinfo=KST),
    )

    assert items[0].value == 18.4
    assert (await client.forecast(nx=60, ny=127, when=datetime(2026, 4, 30, 14, 15, tzinfo=KST)))[
        0
    ].value == 18.4
    assert session.last_params is not None
    assert session.last_params["base_time"] == "1400"


async def test_aio_client_exposes_async_forecast_service() -> None:
    async def run() -> None:
        session = AsyncFakeSession(
            _payload(
                [
                    {"category": "T1H", "obsrValue": "18.4"},
                    {"category": "REH", "obsrValue": "52"},
                    {"category": "PTY", "obsrValue": "0"},
                ]
            )
        )

        async with KmaClient("decoded-key", session=session) as client:
            snapshot = await client.forecast.now(
                nx=60,
                ny=127,
                when=datetime(2026, 4, 30, 14, 45, tzinfo=KST),
            )

        assert snapshot.temperature == 18.4
        assert session.calls[0]["params"]["serviceKey"] == "decoded-key"
        assert session.closed is False
        assert client.closed is True

    await run()


async def test_client_service_key_strips_copied_whitespace() -> None:
    session = FakeSession(_payload({"version": "202604301400"}))
    client = KmaClient(" decoded \n key\t", session=session)

    (await client.version("ODAM", when=datetime(2026, 4, 30, 14, 0, tzinfo=KST)))

    assert session.last_params is not None
    assert session.last_params["serviceKey"] == "decodedkey"


async def test_forecast_uses_latlon_conversion_and_preserves_pcp_labels() -> None:
    session = FakeSession(
        _payload(
            [
                {
                    "baseDate": "20260430",
                    "baseTime": "1400",
                    "fcstDate": "20260430",
                    "fcstTime": "1500",
                    "nx": "60",
                    "ny": "127",
                    "category": "TMP",
                    "fcstValue": "18.4",
                },
                {
                    "baseDate": "20260430",
                    "baseTime": "1400",
                    "fcstDate": "20260430",
                    "fcstTime": "1500",
                    "nx": "60",
                    "ny": "127",
                    "category": "PCP",
                    "fcstValue": "1.0mm 미만",
                },
                {
                    "baseDate": "20260430",
                    "baseTime": "1400",
                    "fcstDate": "20260430",
                    "fcstTime": "1500",
                    "nx": "60",
                    "ny": "127",
                    "category": "SKY",
                    "fcstValue": "1",
                },
            ]
        )
    )
    client = KmaClient("decoded-key", session=session)

    items = await client.forecast(
        lat=37.5665,
        lon=126.9780,
        when=datetime(2026, 4, 30, 14, 15, tzinfo=KST),
    )

    assert session.last_params is not None
    assert session.last_params["nx"] == 60
    assert session.last_params["ny"] == 127
    assert session.last_params["base_time"] == "1400"
    assert items[0].value == 18.4
    assert items[1].value == "1.0mm 미만"
    assert items[1].raw["fcstValue"] == "1.0mm 미만"
    assert items[1].metadata is not None
    assert "serviceKey" not in items[1].metadata.request_params
    assert items[2].label == "맑음"


async def test_client_accepts_standard_location_objects_and_returns_category_enums() -> None:
    session = FakeSession(
        _payload(
            [
                {
                    "baseDate": "20260430",
                    "baseTime": "1400",
                    "fcstDate": "20260430",
                    "fcstTime": "1500",
                    "nx": "60",
                    "ny": "127",
                    "category": "TMP",
                    "fcstValue": "18.4",
                }
            ]
        )
    )
    client = KmaClient("decoded-key", session=session)

    items = await client.forecast(
        location=LatLon(37.5665, 126.9780),
        when=datetime(2026, 4, 30, 14, 15, tzinfo=KST),
    )

    assert session.last_params is not None
    assert session.last_params["nx"] == 60
    assert session.last_params["ny"] == 127
    assert items[0].category is WeatherCategory.TEMPERATURE
    assert items[0].category == "TMP"
    assert items[0].category_enum is WeatherCategory.TEMPERATURE
    assert items[0].unit == "C"
    assert items[0].grid == GridPoint(60, 127)
    assert isinstance(items[0].latlon, LatLon)


async def test_client_accepts_grid_location_mapping() -> None:
    session = FakeSession(
        _payload(
            [
                {"category": "T1H", "obsrValue": "18.4"},
                {"category": "PTY", "obsrValue": "0"},
            ]
        )
    )
    client = KmaClient("decoded-key", session=session)

    snapshot = await client.now(
        location={"nx": "60", "ny": "127"},
        when=datetime(2026, 4, 30, 14, 45, tzinfo=KST),
    )

    assert snapshot.grid == GridPoint(60, 127)
    assert isinstance(snapshot.latlon, LatLon)
    assert snapshot.temperature == 18.4


async def test_fetch_items_accepts_single_item_dict() -> None:
    session = FakeSession(
        _payload(
            {
                "baseDate": "20260430",
                "baseTime": "1430",
                "fcstDate": "20260430",
                "fcstTime": "1500",
                "nx": "60",
                "ny": "127",
                "category": "PTY",
                "fcstValue": "4",
            }
        )
    )
    client = KmaClient("decoded-key", session=session)

    items = await client.forecast_short(
        nx=60, ny=127, when=datetime(2026, 4, 30, 14, 50, tzinfo=KST)
    )

    assert len(items) == 1
    assert items[0].label == "소나기"


async def test_version_converts_aware_datetime_to_kst() -> None:
    session = FakeSession(
        {
            "response": {
                "header": {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"},
                "body": {"items": {"item": []}},
            }
        }
    )
    client = KmaClient("decoded-key", session=session)

    (await client.version("ODAM", datetime(2026, 4, 30, 5, 30, tzinfo=timezone.utc)))

    assert session.last_url is not None
    assert session.last_url.endswith("/getFcstVersion")
    assert session.last_params is not None
    assert session.last_params["basedatetime"] == "202604301430"


async def test_coordinate_validation_rejects_partial_mixed_and_out_of_range_inputs() -> None:
    client = KmaClient("decoded-key", session=FakeSession(_payload([])))

    (await assert_raises(ValueError, lambda: client.now(lat=37.5)))
    (await assert_raises(ValueError, lambda: client.now(nx=60)))
    (await assert_raises(ValueError, lambda: client.now(lat=37.5, lon=127.0, nx=60, ny=127)))
    (await assert_raises(ValueError, lambda: client.now(lat=91.0, lon=127.0)))
    (await assert_raises(ValueError, lambda: client.now(nx=0, ny=127)))
    (await assert_raises(ValueError, lambda: client.now(nx=60, ny=254)))


async def test_result_codes_raise_typed_exceptions() -> None:
    auth_codes = {"20", "30", "31"}
    server_codes = {"04", "99"}
    # `22`는 quota라 아래에서 따로 본다. 예전에는 `12`와 한 묶음이었고
    # `failure_kind`도 `retryable`도 단언하지 않아, `22`가 `retryable=True`로
    # 잘못 분류된 채 이 테스트를 통과했다.
    request_codes = {"12"}

    for code in auth_codes:
        client = KmaClient("bad-key", session=FakeSession(_error_payload(code)))
        error = await assert_raises(KmaAuthError, lambda client=client: client.now(nx=60, ny=127))
        assert error.failure_kind == "auth"
        assert error.result_code == code
        assert error.retryable is False

    for code in server_codes:
        client = KmaClient("decoded-key", session=FakeSession(_error_payload(code)))
        error = await assert_raises(KmaServerError, lambda client=client: client.now(nx=60, ny=127))
        assert error.failure_kind == "server"
        assert error.retryable is True

    for code in request_codes:
        client = KmaClient("decoded-key", session=FakeSession(_error_payload(code)))
        error = await assert_raises(
            KmaRequestError, lambda client=client: client.now(nx=60, ny=127)
        )
        assert error.provider == "data.go.kr"
        assert error.endpoint == "getUltraSrtNcst"
        assert error.failure_kind == "request"
        assert error.retryable is False

    # 일일 quota 초과. 한도는 자정에 리셋되므로 **당일 재시도는 성공할 수 없다** —
    # `retryable=True`면 호출자가 성공 못 할 것에 retry budget을 태운다.
    quota_client = KmaClient("decoded-key", session=FakeSession(_error_payload("22")))
    quota_error = await assert_raises(KmaRequestError, lambda: quota_client.now(nx=60, ny=127))
    assert quota_error.result_code == "22"
    assert quota_error.failure_kind == "quota"
    assert quota_error.retryable is False


async def test_http_200_xml_quota_envelope_preserves_nonretryable_classification() -> None:
    """JSON 요청에도 gateway 오류는 XML 200으로 와서 JSON 분류를 우회할 수 있다."""

    client = KmaClient("decoded-key", session=XmlErrorSession())
    error = await assert_raises(
        KmaRequestError,
        lambda: client.now(nx=60, ny=127),
    )

    assert error.result_code == "22"
    assert error.failure_kind == "quota"
    assert error.retryable is False


async def test_http_200_xml_no_data_envelope_returns_empty_forecast() -> None:
    client = KmaClient("decoded-key", session=XmlNoDataSession())

    items = await client.forecast_short(
        nx=60,
        ny=127,
        when=datetime(2026, 4, 30, 14, 50, tzinfo=KST),
    )

    assert items == []


async def test_no_data_result_code_returns_empty_forecast() -> None:
    client = KmaClient("decoded-key", session=FakeSession(_error_payload("03", "NO_DATA")))

    items = await client.forecast_short(
        nx=60,
        ny=127,
        when=datetime(2026, 4, 30, 14, 50, tzinfo=KST),
    )

    assert items == []


async def test_no_data_result_code_returns_empty_snapshot() -> None:
    client = KmaClient("decoded-key", session=FakeSession(_error_payload("03", "NO_DATA")))

    snapshot = await client.now(nx=60, ny=127, when=datetime(2026, 4, 30, 14, 45, tzinfo=KST))

    assert snapshot.temperature is None
    assert snapshot.humidity is None
    assert snapshot.raw["items"] == []


async def test_malformed_envelope_raises_parse_error() -> None:
    client = KmaClient("decoded-key", session=FakeSession({"not_response": {}}))

    (await assert_raises(KmaParseError, lambda: client.now(nx=60, ny=127)))


async def test_missing_items_raises_parse_error() -> None:
    client = KmaClient(
        "decoded-key",
        session=FakeSession(
            {
                "response": {
                    "header": {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"},
                    "body": {},
                }
            }
        ),
    )

    (await assert_raises(KmaParseError, lambda: client.now(nx=60, ny=127)))


async def test_malformed_forecast_item_raises_parse_error() -> None:
    client = KmaClient(
        "decoded-key",
        session=FakeSession(
            _payload(
                [
                    {
                        "baseDate": "20260430",
                        "baseTime": "1400",
                        "fcstDate": "20260430",
                        "fcstTime": "1500",
                        "nx": "60",
                        "ny": "127",
                        "fcstValue": "18.4",
                    }
                ]
            )
        ),
    )

    (await assert_raises(KmaParseError, lambda: client.forecast(nx=60, ny=127)))


async def test_a_second_page_is_fetched_rather_than_treated_as_a_parse_error() -> None:
    """`getVilageFcst` alone carries a dozen categories over a 3-day, 3-hour
    forecast; how many rows that produces varies with how much the office
    published for that base time, and it does not always fit one page.
    Treating a second page as an unrecoverable parse error turned an
    ordinary large response into a failed run -- for however many
    consecutive base times stayed over the line -- instead of one extra
    request."""
    session = PagedFakeSession(
        {
            1: _paged_payload(
                [
                    {"category": "T1H", "obsrValue": "18.4"},
                    {"category": "REH", "obsrValue": "52"},
                ],
                page_no=1,
                num_of_rows=2,
                total_count=3,
            ),
            2: _paged_payload(
                [{"category": "WSD", "obsrValue": "3.1"}],
                page_no=2,
                num_of_rows=2,
                total_count=3,
            ),
        }
    )
    client = KmaClient("decoded-key", session=session)

    snapshot = await client.now(nx=60, ny=127, when=datetime(2026, 4, 30, 14, 45, tzinfo=KST))

    assert snapshot.temperature == 18.4
    assert snapshot.humidity == 52
    assert snapshot.wind_speed == 3.1
    assert session.requested_pages == [1, 2]
    assert snapshot.metadata is not None
    # The paginated request differs from page to page only in pageNo; the
    # metadata describes what identifies the fetch (base_date/base_time/grid),
    # which is the same value on every page.
    assert snapshot.metadata.base_time == "1400"


async def test_pagination_gives_up_after_the_page_cap_rather_than_looping_forever() -> None:
    """A response that never reports itself as the last page must fail
    loudly and boundedly, not hang the run consuming pages one at a time."""

    class NeverLastPageSession:
        def __init__(self) -> None:
            self.calls = 0

        async def get(
            self, url: str, *, params: dict[str, Any], timeout: float
        ) -> FakeResponse:
            self.calls += 1
            page_no = int(params["pageNo"])
            # totalCount always claims one more row than this page reports,
            # so has_next_page is true no matter how many pages are fetched.
            return FakeResponse(
                _paged_payload(
                    [{"category": "T1H", "obsrValue": "18.4"}],
                    page_no=page_no,
                    num_of_rows=1,
                    total_count=page_no + 1,
                )
            )

    session = NeverLastPageSession()
    client = KmaClient("decoded-key", session=session)

    exc = await assert_raises(KmaParseError, lambda: client.now(nx=60, ny=127))

    assert "paginat" in str(exc).lower()
    # Bounded: the client gave up rather than fetching pages indefinitely.
    assert session.calls <= 21
