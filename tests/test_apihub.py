from __future__ import annotations

import asyncio
import json
import warnings
from typing import Any, Callable

import httpx

from kma.apihub import (
    ApiHubClient,
    AsyncApiHubClient,
    detect_image_info,
    extract_apihub_endpoints,
    parse_apihub_sample_url,
    parse_apihub_services,
    parse_apihub_text_table,
    redact_url_credentials,
)
from kma.exceptions import KmaAuthError
from kma.pagination import PaginationLimitWarning


class FakeResponse:
    def __init__(
        self,
        text: str,
        *,
        url: str = "https://apihub.kma.go.kr/api/test",
        status_code: int = 200,
        content_type: str = "text/plain; charset=UTF-8",
    ) -> None:
        self.text = text
        self.content = text.encode("utf-8")
        self.url = url
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {}


class FakeErrorResponse(FakeResponse):
    def __init__(self, text: str, *, status_code: int = 403) -> None:
        super().__init__(text, status_code=status_code, content_type="application/json")

    def raise_for_status(self) -> None:
        request = httpx.Request("GET", self.url)
        response = httpx.Response(
            self.status_code,
            request=request,
            json={"result": {"status": self.status_code, "message": "활용신청이 필요합니다"}},
        )
        raise httpx.HTTPStatusError("HTTP error", request=request, response=response)

    def json(self) -> dict[str, Any]:
        return {"result": {"status": self.status_code, "message": "활용신청이 필요합니다."}}


class FakeSession:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, *, params: dict[str, Any] | None, timeout: float) -> FakeResponse:
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return FakeResponse(self.text, url=url)


class AsyncFakeSession:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None,
        timeout: float,
    ) -> FakeResponse:
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return FakeResponse(self.text, url=url)


class FakeErrorSession(FakeSession):
    def get(self, url: str, *, params: dict[str, Any] | None, timeout: float) -> FakeResponse:
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return FakeErrorResponse("error")


def _open_api_page_body(*, page_no: int, num_of_rows: int, total_count: int) -> str:
    start = (page_no - 1) * num_of_rows + 1
    end = min(page_no * num_of_rows, total_count)
    items = [{"id": f"item-{i}"} for i in range(start, end + 1)]
    payload = {
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
    return json.dumps(payload)


class PagingFakeSession:
    """`pageNo` 요청 파라미터에 따라 서로 다른 body를 돌려주는 fake open_api session."""

    def __init__(self, *, total_count: int, num_of_rows: int = 10) -> None:
        self.total_count = total_count
        self.num_of_rows = num_of_rows
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, *, params: dict[str, Any] | None, timeout: float) -> FakeResponse:
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        assert params is not None
        page_no = int(params["pageNo"])
        body = _open_api_page_body(
            page_no=page_no,
            num_of_rows=self.num_of_rows,
            total_count=self.total_count,
        )
        return FakeResponse(body, url=url, content_type="application/json")


class AsyncPagingFakeSession(PagingFakeSession):
    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None,
        timeout: float,
    ) -> FakeResponse:
        return super().get(url, params=params, timeout=timeout)


def assert_raises(exc_type: type[BaseException], func: Callable[[], object]) -> BaseException:
    try:
        func()
    except exc_type as exc:
        return exc
    raise AssertionError(f"expected {exc_type.__name__}")


def test_parse_apihub_services_from_portal_script() -> None:
    html = """
    <script>
    const apiList = [
      {"seqApi":238,"nmApi":"종관기상관측(ASOS)"},
      {"seqApi":239,"nmApi":"방재기상관측(AWS)"}
    ];
    </script>
    """

    services = parse_apihub_services(html, 2)

    assert [service.service_id for service in services] == [238, 239]
    assert services[0].category_name == "지상관측"
    assert services[0].service_name == "종관기상관측(ASOS)"


def test_parse_apihub_sample_url_removes_auth_key_and_preserves_params() -> None:
    endpoint = parse_apihub_sample_url(
        "https://apihub.kma.go.kr/api/typ01/url/kma_sfctm2.php?"
        "tm=202211300900&amp;stn=0&amp;help=1&amp;authKey=secret"
    )

    assert endpoint.path == "/api/typ01/url/kma_sfctm2.php"
    assert endpoint.parameters == ("tm", "stn", "help")
    assert endpoint.sample_params == {"tm": "202211300900", "stn": "0", "help": "1"}
    assert endpoint.query_parts == (("named", "tm"), ("named", "stn"), ("named", "help"))


def test_parse_apihub_sample_url_supports_bare_query_parts() -> None:
    endpoint = parse_apihub_sample_url(
        "https://apihub.kma.go.kr/api/typ03/cgi/aws3/nph-awsm_tms_h06?"
        "202305031000&0&108,419&m&_DT=RSW:AWSCHART&authKey=secret"
    )

    assert endpoint.path == "/api/typ03/cgi/aws3/nph-awsm_tms_h06"
    assert endpoint.parameters == ("arg1", "arg2", "arg3", "arg4", "_DT")
    assert endpoint.sample_params == {
        "arg1": "202305031000",
        "arg2": "0",
        "arg3": "108,419",
        "arg4": "m",
        "_DT": "RSW:AWSCHART",
    }
    assert endpoint.query_parts == (
        ("bare", "arg1"),
        ("bare", "arg2"),
        ("bare", "arg3"),
        ("bare", "arg4"),
        ("named", "_DT"),
    )


def test_extract_apihub_endpoints_deduplicates_generated_urls() -> None:
    html = """
    https://apihub.kma.go.kr/api/typ01/url/wrn_reg.php?tmfc=0&amp;authKey=a
    https://apihub.kma.go.kr/api/typ01/url/wrn_reg.php?tmfc=0&authKey=a
    https://apihub.kma.go.kr/api/typ01/url/wrn_met_data.php?reg=0&wrn=A&authKey=a
    """

    endpoints = extract_apihub_endpoints(html)

    assert len(endpoints) == 2
    assert endpoints[0].path == "/api/typ01/url/wrn_reg.php"
    assert endpoints[1].parameters == ("reg", "wrn")


def test_apihub_request_path_appends_auth_key() -> None:
    session = FakeSession("ok")
    client = ApiHubClient("hub-key", session=session)

    response = client.request_path("/api/typ01/url/kma_sfctm2.php", {"tm": "202211300900"})

    assert response.text == "ok"
    assert response.metadata is not None
    assert response.metadata.provider == "apihub"
    assert response.metadata.endpoint == "/api/typ01/url/kma_sfctm2.php"
    assert response.metadata.request_params == {"tm": "202211300900"}
    assert session.calls[0]["url"] == "https://apihub.kma.go.kr/api/typ01/url/kma_sfctm2.php"
    assert session.calls[0]["params"] == {"authKey": "hub-key", "tm": "202211300900"}


def test_apihub_async_request_path_appends_auth_key() -> None:
    async def run() -> None:
        session = AsyncFakeSession("ok")
        client = ApiHubClient("hub-key", async_session=session)

        response = await client.arequest_path(
            "/api/typ01/url/kma_sfctm2.php",
            {"tm": "202211300900"},
        )

        assert response.text == "ok"
        assert session.calls[0]["url"] == "https://apihub.kma.go.kr/api/typ01/url/kma_sfctm2.php"
        assert session.calls[0]["params"] == {"authKey": "hub-key", "tm": "202211300900"}

    asyncio.run(run())


def test_apihub_aio_returns_async_facade() -> None:
    async def run() -> None:
        session = AsyncFakeSession("ok")
        client = ApiHubClient.aio("hub-key", async_session=session)

        assert isinstance(client, AsyncApiHubClient)
        assert client.auth_key == "hub-key"

        async with client:
            response = await client.request_path(
                "/api/typ01/url/kma_sfctm2.php",
                {"tm": "202211300900"},
            )

        assert response.text == "ok"
        assert session.calls[0]["params"] == {"authKey": "hub-key", "tm": "202211300900"}
        assert client.closed is True

    asyncio.run(run())


def test_apihub_auth_key_strips_copied_whitespace() -> None:
    session = FakeSession("ok")
    client = ApiHubClient(" hub \n key\t", session=session)

    client.request_path("/api/typ01/url/kma_sfctm2.php")

    assert session.calls[0]["params"] == {"authKey": "hubkey"}


def test_apihub_open_api_builds_typ02_path_and_defaults() -> None:
    session = FakeSession('{"response": "ok"}')
    client = ApiHubClient("hub-key", session=session)

    response = client.open_api(
        "MidFcstInfoService",
        "getMidFcst",
        {"stnId": "108", "tmFc": "202605010600"},
    )

    assert response.json() == {"response": "ok"}
    assert session.calls[0]["url"].endswith("/api/typ02/openApi/MidFcstInfoService/getMidFcst")
    assert session.calls[0]["params"]["authKey"] == "hub-key"
    assert session.calls[0]["params"]["dataType"] == "JSON"
    assert session.calls[0]["params"]["pageNo"] == 1
    assert session.calls[0]["params"]["numOfRows"] == 10


def test_apihub_request_query_parts_preserves_legacy_bare_query() -> None:
    session = FakeSession("image")
    client = ApiHubClient("hub-key", session=session)

    client.request_query_parts(
        "/api/typ03/cgi/aws3/nph-awsm_tms_h06",
        (("bare", "tm"), ("bare", "mode"), ("named", "_DT")),
        {"tm": "202305031000", "mode": "0", "_DT": "RSW:AWSCHART"},
    )

    assert session.calls[0]["url"] == (
        "https://apihub.kma.go.kr/api/typ03/cgi/aws3/nph-awsm_tms_h06"
        "?202305031000&0&_DT=RSW:AWSCHART&authKey=hub-key"
    )
    assert session.calls[0]["params"] is None


def test_apihub_rejects_non_api_paths() -> None:
    client = ApiHubClient("hub-key", session=FakeSession("ok"))

    assert_raises(ValueError, lambda: client.request_path("/noticeList.do"))


def test_apihub_403_maps_to_auth_error_without_chained_url() -> None:
    client = ApiHubClient("hub-key", session=FakeErrorSession("error"))

    error = assert_raises(
        KmaAuthError,
        lambda: client.request_path("/api/typ01/url/kma_sfctm2.php", {"tm": "202211300900"}),
    )

    assert "403" in str(error)
    assert "활용신청" in str(error)
    assert "hub-key" not in str(error)
    assert error.__cause__ is None


def test_parse_apihub_text_table_uses_comment_header() -> None:
    table = parse_apihub_text_table(
        "# TM STN TA\n"
        "202605010000 108 17.5\n"
        "202605010100 108 18.0\n"
    )

    assert table.headers == ("TM", "STN", "TA")
    assert table.rows[0]["TM"] == "202605010000"
    assert table.rows[1]["TA"] == "18.0"
    assert table.comments == ("# TM STN TA",)


def test_parse_apihub_text_table_supports_csv_delimiter() -> None:
    table = parse_apihub_text_table("code,name\n108,서울\n159,부산\n", delimiter=",")

    assert table.headers == ("code", "name")
    assert table.rows == ({"code": "108", "name": "서울"}, {"code": "159", "name": "부산"})


def test_detect_image_info_reads_png_dimensions() -> None:
    content = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\r"
        b"IHDR"
        + (640).to_bytes(4, "big")
        + (480).to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
    )

    assert detect_image_info(content) == ("png", 640, 480)


def test_redact_url_credentials_preserves_bare_query_parts() -> None:
    redacted = redact_url_credentials(
        "https://apihub.kma.go.kr/api/typ03/cgi/aws3/nph-awsm_tms_h06?"
        "202305031000&0&_DT=RSW:AWSCHART&authKey=secret"
    )

    assert redacted == (
        "https://apihub.kma.go.kr/api/typ03/cgi/aws3/nph-awsm_tms_h06?"
        "202305031000&0&_DT=RSW:AWSCHART&authKey=***"
    )


def test_redact_url_credentials_handles_generic_key_name() -> None:
    assert (
        redact_url_credentials("https://example.com/openapi/weather?key=secret&type=json")
        == "https://example.com/openapi/weather?key=***&type=json"
    )


def test_apihub_discover_services_and_endpoints_use_portal_pages() -> None:
    service_html = 'const apiList = [{"seqApi":288,"nmApi":"기상특보"}];'
    endpoint_html = "https://apihub.kma.go.kr/api/typ01/url/wrn_reg.php?tmfc=0&authKey=secret"
    session = FakeSession(service_html)
    client = ApiHubClient("hub-key", session=session)

    services = client.discover_services((10,))
    session.text = endpoint_html
    endpoints = client.discover_endpoints(services[0].category_id, services[0].service_id)

    assert services[0].service_name == "기상특보"
    assert endpoints[0].path == "/api/typ01/url/wrn_reg.php"
    assert session.calls[0]["url"] == "https://apihub.kma.go.kr/apiList.do"
    assert session.calls[0]["params"] == {"seqApi": 10}
    assert session.calls[1]["params"] == {"seqApi": 10, "seqApiSub": 288}


def test_apihub_iter_pages_collects_all_pages_without_warning() -> None:
    session = PagingFakeSession(total_count=25, num_of_rows=10)
    client = ApiHubClient("hub-key", session=session)

    with warnings.catch_warnings():
        warnings.simplefilter("error", PaginationLimitWarning)
        pages = list(
            client.iter_pages("MidFcstInfoService", "getMidFcst", num_of_rows=10)
        )

    assert [page["pageNo"] for page in pages] == [1, 2, 3]
    assert len(pages[-1]["items"]["item"]) == 5


def test_apihub_aiter_pages_collects_all_pages_without_warning() -> None:
    async def run() -> list[dict[str, Any]]:
        session = AsyncPagingFakeSession(total_count=25, num_of_rows=10)
        client = ApiHubClient("hub-key", async_session=session)
        pages = []
        async for page in client.aiter_pages(
            "MidFcstInfoService", "getMidFcst", num_of_rows=10
        ):
            pages.append(page)
        return pages

    with warnings.catch_warnings():
        warnings.simplefilter("error", PaginationLimitWarning)
        pages = asyncio.run(run())

    assert [page["pageNo"] for page in pages] == [1, 2, 3]
    assert len(pages[-1]["items"]["item"]) == 5


def test_apihub_aiter_pages_warns_on_truncation_like_sync_iter_pages() -> None:
    """비동기 aiter_pages는 동기 iter_pages와 동일하게 max_pages 절단을 경고해야 한다.

    회귀 방지 대상: 이전에는 aiter_pages가 pagination.aiter_pages를 거치지 않고
    직접 루프를 구현해 PaginationLimitWarning을 내지 않고 조용히 데이터를 잘랐다.
    """

    sync_session = PagingFakeSession(total_count=50, num_of_rows=10)
    sync_client = ApiHubClient("hub-key", session=sync_session)
    with warnings.catch_warnings(record=True) as sync_caught:
        warnings.simplefilter("always")
        sync_pages = list(
            sync_client.iter_pages(
                "MidFcstInfoService", "getMidFcst", num_of_rows=10, max_pages=2
            )
        )

    async def run_async() -> tuple[list[dict[str, Any]], list[warnings.WarningMessage]]:
        async_session = AsyncPagingFakeSession(total_count=50, num_of_rows=10)
        async_client = ApiHubClient("hub-key", async_session=async_session)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            pages = [
                page
                async for page in async_client.aiter_pages(
                    "MidFcstInfoService", "getMidFcst", num_of_rows=10, max_pages=2
                )
            ]
        return pages, caught

    async_pages, async_caught = asyncio.run(run_async())

    assert len(sync_pages) == 2
    assert len(async_pages) == 2
    assert any(issubclass(w.category, PaginationLimitWarning) for w in sync_caught)
    assert any(issubclass(w.category, PaginationLimitWarning) for w in async_caught)


def test_apihub_aiter_pages_validates_arguments_like_sync_iter_pages() -> None:
    session = PagingFakeSession(total_count=10, num_of_rows=10)
    sync_client = ApiHubClient("hub-key", session=session)
    sync_error = assert_raises(
        ValueError,
        lambda: list(
            sync_client.iter_pages("MidFcstInfoService", "getMidFcst", max_pages=0)
        ),
    )
    assert "max_pages" in str(sync_error)

    async def run() -> BaseException:
        async_session = AsyncPagingFakeSession(total_count=10, num_of_rows=10)
        async_client = ApiHubClient("hub-key", async_session=async_session)
        try:
            async for _ in async_client.aiter_pages(
                "MidFcstInfoService", "getMidFcst", max_pages=0
            ):
                pass
        except ValueError as exc:
            return exc
        raise AssertionError("expected ValueError")

    async_error = asyncio.run(run())
    assert "max_pages" in str(async_error)
