"""기상청 APIHub 범용 클라이언트와 탐색 도우미."""

from __future__ import annotations

import csv
import html
import io
import json
import re
import time
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass
from functools import cached_property
from typing import Any
from urllib.parse import quote_plus, unquote_plus, urlsplit, urlunsplit

import httpx

from ._credentials import APIHUB_ENV_NAMES, first_env_value, normalize_api_key
from ._http import (
    NO_DATA_RESULT_CODE,
    build_async_client,
    get_with_retries,
    raise_for_kma_http_error,
    raise_for_kma_network_error,
    raise_for_kma_result_code,
    raise_for_kma_xml_error_body,
    validate_async_session,
)
from ._ratelimit import AsyncTokenBucket
from ._redact import credential_values, redact_debug, redact_exception
from .debug import DebugRun, debug_error, redact_sensitive
from .exceptions import KmaError, KmaParseError
from .metadata import (
    ResponseMetadata,
    is_credential_param,
    make_response_metadata,
    redact_credentials_in_text,
    request_params_from_url,
)
from .pagination import iter_pages as _iter_pages

APIHUB_BASE_URL = "https://apihub.kma.go.kr"
_APIHUB_ALLOWED_HOSTS = frozenset({"apihub.kma.go.kr"})
APIHUB_CATEGORY_IDS = (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 14, 15)

APIHUB_CATEGORIES: dict[int, str] = {
    2: "지상관측",
    3: "해양관측",
    4: "고층관측",
    5: "레이더",
    6: "위성",
    7: "지진/화산",
    8: "태풍",
    9: "수치모델",
    10: "예특보",
    11: "응용기상",
    12: "세계기상",
    13: "산업특화",
    14: "항공기상",
    15: "기후변화",
}


@dataclass(frozen=True)
class ApiHubService:
    category_id: int
    category_name: str
    service_id: int
    service_name: str


@dataclass(frozen=True)
class ApiHubEndpoint:
    path: str
    parameters: tuple[str, ...]
    sample_params: Mapping[str, str]
    query_parts: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ApiHubEndpointSpec:
    name: str
    title: str
    category_id: int
    category_name: str
    service_id: int
    service_name: str
    path: str
    parameters: tuple[str, ...]
    sample_params: Mapping[str, str]
    query_parts: tuple[tuple[str, str], ...]
    response_kind: str
    source: str


@dataclass(frozen=True)
class ApiHubAttachment:
    title: str
    url: str
    filename: str
    category_id: int
    category_name: str
    service_id: int
    service_name: str
    kind: str


@dataclass(frozen=True)
class ApiHubTextTable:
    headers: tuple[str, ...]
    rows: tuple[Mapping[str, str], ...]
    comments: tuple[str, ...]
    raw_lines: tuple[str, ...]


@dataclass(frozen=True)
class ApiHubImage:
    content: bytes
    content_type: str
    format: str | None
    width: int | None
    height: int | None


@dataclass(frozen=True)
class ApiHubResponse:
    url: str
    status_code: int
    content_type: str
    content: bytes
    metadata: ResponseMetadata | None = None

    @cached_property
    def text(self) -> str:
        return self.content.decode(_charset_from_content_type(self.content_type), errors="replace")

    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except ValueError as exc:
            raise KmaParseError(
                "APIHub response was not JSON",
                provider=self.metadata.provider if self.metadata else "apihub",
                endpoint=self.metadata.endpoint if self.metadata else None,
                status_code=self.status_code,
                failure_kind="parse",
                retryable=False,
            ) from exc

    def text_table(self, delimiter: str | None = None) -> ApiHubTextTable:
        return parse_apihub_text_table(self.text, delimiter=delimiter)

    def image(self) -> ApiHubImage:
        image_format, width, height = detect_image_info(self.content)
        return ApiHubImage(
            content=self.content,
            content_type=self.content_type,
            format=image_format,
            width=width,
            height=height,
        )


class ApiHubClient:
    """기상청 APIHub `authKey` API용 범용 클라이언트.

    APIHub는 text, JSON, XML, image, file endpoint를 함께 제공합니다.
    모든 endpoint의 schema가 같다고 가정하지 않고, 범용 path 호출과
    탐색 helper를 제공합니다.
    """

    def __init__(
        self,
        auth_key: str,
        *,
        timeout: float = 20,
        retries: int = 3,
        base_url: str = APIHUB_BASE_URL,
        session: Any | None = None,
        max_rps: float = 5.0,
        rate_limiter: AsyncTokenBucket | None = None,
    ) -> None:
        self.auth_key = normalize_api_key(auth_key, field_name="auth_key")
        self.timeout = timeout
        self.retries = retries
        self.base_url = _validate_apihub_base_url(base_url)
        self._session = session
        self._owns_session = session is None
        validate_async_session(session)
        self.rate_limiter = rate_limiter if rate_limiter is not None else AsyncTokenBucket(max_rps)
        self.closed = False

    def _ensure_open(self) -> None:
        if self.closed:
            raise RuntimeError("client is closed")

    @property
    def session(self) -> Any:
        return self._get_session()

    async def aclose(self) -> None:
        if not self.closed:
            self.closed = True
            if self._owns_session and self._session is not None:
                close = getattr(self._session, "aclose", None)
                if callable(close):
                    await close()

    @classmethod
    def from_env(cls, name: str = "KMA_APIHUB_AUTH_KEY", **kwargs: Any) -> ApiHubClient:
        auth_key = first_env_value((name, *APIHUB_ENV_NAMES))
        return cls(auth_key, **kwargs)

    async def __aenter__(self) -> ApiHubClient:
        self._ensure_open()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def request_path(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
    ) -> ApiHubResponse:
        """Asynchronously call an APIHub `/api/...` path."""

        clean_path = _normalize_apihub_path(path)
        request_params: dict[str, Any] = {}
        if params:
            request_params.update(params)
        request_params["authKey"] = self.auth_key
        return await self._get(clean_path, request_params)

    async def request_query_parts(
        self,
        path: str,
        query_parts: Iterable[tuple[str, str]],
        params: Mapping[str, Any] | None = None,
    ) -> ApiHubResponse:
        """Asynchronously call an APIHub endpoint with ordered query parts."""

        clean_path = _normalize_apihub_path(path)
        values = dict(params or {})
        fragments: list[str] = []
        for kind, name in query_parts:
            if name == "authKey":
                continue
            if name not in values:
                continue
            value = values[name]
            if value is None:
                continue
            if kind == "bare":
                fragments.append(_quote_query_value(value))
            else:
                fragments.append(f"{quote_plus(name)}={_quote_query_value(value)}")
        fragments.append(f"authKey={_quote_query_value(self.auth_key)}")
        return await self._get_raw(f"{clean_path}?{'&'.join(fragments)}")

    async def open_api(
        self,
        service: str,
        operation: str,
        params: Mapping[str, Any] | None = None,
        *,
        data_type: str = "JSON",
        page_no: int = 1,
        num_of_rows: int = 10,
    ) -> ApiHubResponse:
        """Asynchronously call `/api/typ02/openApi/{service}/{operation}`."""

        request_params: dict[str, Any] = {
            "pageNo": page_no,
            "numOfRows": num_of_rows,
            "dataType": data_type,
        }
        if params:
            request_params.update(params)
        endpoint = f"/api/typ02/openApi/{service.strip('/')}/{operation.strip('/')}"
        response = await self.request_path(endpoint, request_params)
        try:
            _check_apihub_result_code(response, endpoint=endpoint)
        except KmaError as exc:
            redact_exception(exc, self.auth_key, *credential_values(params))
            raise exc from None
        return response

    async def discover_services(
        self,
        category_ids: tuple[int, ...] = APIHUB_CATEGORY_IDS,
    ) -> list[ApiHubService]:
        """Asynchronously fetch APIHub service metadata."""
        try:
            services: list[ApiHubService] = []
            for category_id in category_ids:
                response = await self._portal_get("/apiList.do", {"seqApi": category_id})
                services.extend(parse_apihub_services(response.text, category_id))
            return services
        except KmaError as exc:
            redact_exception(exc, self.auth_key, *credential_values(None))
            raise exc from None

    async def discover_endpoints(
        self,
        category_id: int,
        service_id: int,
    ) -> list[ApiHubEndpoint]:
        """Asynchronously fetch endpoint samples for one APIHub service page."""

        response = await self._portal_get(
            "/apiList.do",
            {"seqApi": category_id, "seqApiSub": service_id},
        )
        return extract_apihub_endpoints(response.text)

    async def debug_fetch_endpoint(
        self,
        spec: ApiHubEndpointSpec,
        params: Mapping[str, Any] | None = None,
        *,
        use_sample: bool = False,
    ) -> DebugRun:
        """디버그 UI/fixture 생성을 위해 APIHub endpoint 하나를 호출합니다.

        `spec`은 카탈로그(`apihub_endpoint_catalog()`)나
        `ApiHubGeneratedClient.endpoint(name)`에서 얻은 `ApiHubEndpointSpec`
        입니다. 여기서는 어떤 endpoint인지에 따라 분기하지 않고, `spec`이 담은
        `path`/`query_parts`/`response_kind` 메타데이터로만 요청을 만들고 결과를
        해석합니다 — 470개 endpoint 전부가 같은 경로를 지납니다.

        일부 legacy endpoint는 이름 없는 query 조각(``query_parts``에
        ``"bare"`` 항목)을 쓰므로, 그 경우에만 `request_query_parts`로,
        나머지는 `request_path`로 호출을 위임합니다(``ApiHubGeneratedClient
        .call_endpoint``와 같은 판정 규칙).
        """
        try:
            request_params: dict[str, Any] = {}
            if use_sample:
                request_params.update(spec.sample_params)
            if params:
                request_params.update(params)

            input_data = redact_sensitive(
                {
                    "endpoint": spec.name,
                    "params": request_params,
                    "use_sample": use_sample,
                }
            )
            trace = [
                f"APIHub {spec.name} ({spec.path}) 호출 준비",
                f"response_kind={spec.response_kind}",
            ]
            request_info = redact_sensitive(
                {
                    "method": "GET",
                    "url": f"{self.base_url}{spec.path}",
                    "query": request_params,
                }
            )

            started_at = time.monotonic()
            try:
                if any(kind == "bare" for kind, _name in spec.query_parts):
                    response = await self.request_query_parts(
                        spec.path, spec.query_parts, request_params
                    )
                else:
                    response = await self.request_path(spec.path, request_params)
            except Exception as exc:
                elapsed_ms = (time.monotonic() - started_at) * 1000
                trace.append(f"요청 실패: {exc.__class__.__name__} ({elapsed_ms:.0f}ms)")
                return redact_debug(
                    DebugRun(
                        function=spec.name,
                        input=input_data,
                        request=request_info,
                        response={},
                        parsed=None,
                        processed=None,
                        trace=trace,
                        error=debug_error(exc),
                    ),
                    self.auth_key,
                    *credential_values(params),
                )

            elapsed_ms = (time.monotonic() - started_at) * 1000
            trace.append(
                f"응답 수신: HTTP {response.status_code}, {len(response.content)} bytes "
                f"({elapsed_ms:.0f}ms)"
            )
            parsed, processed = _debug_parse_apihub_response(response, spec.response_kind)
            return redact_debug(
                DebugRun(
                    function=spec.name,
                    input=input_data,
                    request=request_info,
                    response={
                        "status_code": response.status_code,
                        "content_type": response.content_type,
                        "body": parsed,
                    },
                    parsed=parsed,
                    processed=processed,
                    trace=trace,
                ),
                self.auth_key,
                *credential_values(params),
            )
        except KmaError as exc:
            redact_exception(exc, self.auth_key, *credential_values(params))
            raise exc from None

    async def iter_pages(
        self,
        service: str,
        operation: str,
        params: Mapping[str, Any] | None = None,
        *,
        data_type: str = "JSON",
        start_page: int = 1,
        num_of_rows: int = 10,
        max_pages: int = 100,
        max_items: int | None = None,
    ) -> AsyncIterator[Mapping[str, Any]]:
        """Asynchronously iterate paginated APIHub `open_api` response bodies."""
        try:
            endpoint = f"/api/typ02/openApi/{service.strip('/')}/{operation.strip('/')}"

            async def _fetch_page(page_no: int) -> Mapping[str, Any]:
                response = await self.open_api(
                    service,
                    operation,
                    params,
                    data_type=data_type,
                    page_no=page_no,
                    num_of_rows=num_of_rows,
                )
                return _apihub_open_api_body(response, endpoint=endpoint)

            async for body in _iter_pages(
                _fetch_page,
                start_page=start_page,
                max_pages=max_pages,
                max_items=max_items,
            ):
                yield body
        except KmaError as exc:
            redact_exception(exc, self.auth_key, *credential_values(params))
            raise exc from None

    async def _portal_get(self, path: str, params: Mapping[str, Any]) -> ApiHubResponse:
        return await self._get(path, params)

    async def _get(self, path: str, params: Mapping[str, Any]) -> ApiHubResponse:
        return await self._get_url(
            f"{self.base_url}/{path.lstrip('/')}",
            params=dict(params),
        )

    async def _get_raw(self, path_with_query: str) -> ApiHubResponse:
        return await self._get_url(f"{self.base_url}/{path_with_query.lstrip('/')}", params=None)

    async def _get_url(self, url: str, params: Mapping[str, Any] | None) -> ApiHubResponse:
        try:
            endpoint = urlsplit(url).path
            metadata = make_response_metadata(
                provider="apihub",
                service_name="APIHub",
                endpoint=endpoint,
                request_params=params if params is not None else request_params_from_url(url),
            )
            try:
                response = await get_with_retries(
                    self._get_session(),
                    url,
                    params=dict(params) if params is not None else None,
                    timeout=self.timeout,
                    retries=self.retries,
                    rate_limiter=self.rate_limiter,
                    ensure_open=self._ensure_open,
                )
            except httpx.HTTPStatusError as exc:
                raise_for_kma_http_error(
                    exc,
                    provider="apihub",
                    endpoint=endpoint,
                    label="APIHub",
                    detail=_response_error_message(exc.response),
                )
            except httpx.RequestError:
                raise_for_kma_network_error(
                    provider="apihub",
                    endpoint=endpoint,
                    label="APIHub",
                )

            content_type = response.headers.get("Content-Type", "")
            return ApiHubResponse(
                url=redact_url_credentials(str(response.url)),
                status_code=response.status_code,
                content_type=content_type,
                content=response.content,
                metadata=metadata,
            )
        except KmaError as exc:
            redact_exception(exc, self.auth_key, *credential_values(params))
            raise exc from None

    def _get_session(self) -> Any:
        self._ensure_open()
        if self._session is None:
            self._session = build_async_client()
        return self._session


def parse_apihub_services(html_text: str, category_id: int) -> list[ApiHubService]:
    """APIHub의 `const apiList = [...]` service 목록을 파싱합니다."""

    match = re.search(r"const\s+apiList\s*=\s*(\[.*?\]);", html_text, re.S)
    if not match:
        return []
    try:
        raw_services = json.loads(match.group(1))
    except ValueError as exc:
        raise KmaParseError(
            "Could not parse APIHub apiList JSON",
            provider="apihub",
            endpoint="/apiList.do",
            failure_kind="parse",
            retryable=False,
        ) from exc

    category_name = APIHUB_CATEGORIES.get(category_id, str(category_id))
    services: list[ApiHubService] = []
    for raw in raw_services:
        try:
            services.append(
                ApiHubService(
                    category_id=category_id,
                    category_name=category_name,
                    service_id=int(raw["seqApi"]),
                    service_name=str(raw["nmApi"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise KmaParseError(
                f"Malformed APIHub service entry: {raw!r}",
                provider="apihub",
                endpoint="/apiList.do",
                failure_kind="parse",
                retryable=False,
            ) from exc
    return services


def extract_apihub_endpoints(html_text: str) -> list[ApiHubEndpoint]:
    """APIHub service page에서 생성 API 예제 URL을 추출합니다."""

    endpoints: list[ApiHubEndpoint] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for raw in re.findall(r"https://apihub\.kma\.go\.kr/api/[^\s\"<>]+", html_text):
        endpoint = parse_apihub_sample_url(raw)
        key = (endpoint.path, endpoint.parameters)
        if key not in seen:
            endpoints.append(endpoint)
            seen.add(key)
    return endpoints


def parse_apihub_sample_url(raw_url: str) -> ApiHubEndpoint:
    """APIHub 예제 URL 하나를 path, parameter, 예제 값으로 파싱합니다."""

    cleaned = html.unescape(raw_url).replace("&amp;", "&")
    parts = urlsplit(cleaned)
    sample_params: dict[str, str] = {}
    parameters: list[str] = []
    query_parts = _parse_query_parts(parts.query)
    for kind, key in query_parts:
        if key == "authKey":
            continue
        value = _query_part_value(parts.query, kind, key)
        if key not in sample_params:
            parameters.append(key)
            sample_params[key] = value
    return ApiHubEndpoint(parts.path, tuple(parameters), sample_params, query_parts)


def parse_apihub_text_table(text: str, delimiter: str | None = None) -> ApiHubTextTable:
    """일반적인 APIHub text 응답을 comment와 dict row로 파싱합니다.

    APIHub text endpoint는 하나의 format으로 통일되어 있지 않습니다. delimiter가
    주어지면 CSV를 처리하고, header를 찾을 수 있는 공백 table을 처리하며,
    신뢰할 수 있는 header가 없으면 `_raw` row로 되돌립니다.
    """

    raw_lines = tuple(line.rstrip("\r") for line in text.splitlines())
    nonempty = [line.strip() for line in raw_lines if line.strip()]
    comments = tuple(line for line in nonempty if line.startswith("#"))
    data_lines = [line for line in nonempty if not line.startswith("#")]
    if not data_lines:
        return ApiHubTextTable((), (), comments, raw_lines)

    if delimiter is not None:
        reader = csv.DictReader(io.StringIO("\n".join(data_lines)), delimiter=delimiter)
        headers = tuple(reader.fieldnames or ())
        rows = tuple(dict(row) for row in reader)
        return ApiHubTextTable(headers, rows, comments, raw_lines)

    headers = _guess_text_headers(comments, data_lines)
    if not headers:
        rows = tuple({"_raw": line} for line in data_lines)
        return ApiHubTextTable((), rows, comments, raw_lines)

    rows_list: list[Mapping[str, str]] = []
    for line in data_lines:
        values = line.split()
        if len(values) < len(headers):
            rows_list.append({"_raw": line})
            continue
        if len(values) > len(headers):
            values = values[: len(headers) - 1] + [" ".join(values[len(headers) - 1 :])]
        rows_list.append(dict(zip(headers, values)))
    return ApiHubTextTable(headers, tuple(rows_list), comments, raw_lines)


def detect_image_info(content: bytes) -> tuple[str | None, int | None, int | None]:
    """일반적인 APIHub image bytes의 format과 pixel 크기를 반환합니다."""

    if content.startswith(b"\x89PNG\r\n\x1a\n") and len(content) >= 24:
        return (
            "png",
            int.from_bytes(content[16:20], "big"),
            int.from_bytes(content[20:24], "big"),
        )
    if content[:6] in (b"GIF87a", b"GIF89a") and len(content) >= 10:
        return (
            "gif",
            int.from_bytes(content[6:8], "little"),
            int.from_bytes(content[8:10], "little"),
        )
    if content.startswith(b"\xff\xd8"):
        size = _detect_jpeg_size(content)
        if size is not None:
            return "jpeg", size[0], size[1]
        return "jpeg", None, None
    return None, None, None


def redact_url_credentials(url: str) -> str:
    """API credential query 값을 마스킹한 URL을 반환합니다."""

    parts = urlsplit(url)
    if not parts.query:
        return url
    redacted_parts: list[str] = []
    for raw_part in parts.query.split("&"):
        if "=" not in raw_part:
            redacted_parts.append(raw_part)
            continue
        key, _value = raw_part.split("=", 1)
        if is_credential_param(unquote_plus(key)):
            redacted_parts.append(f"{key}=***")
        else:
            redacted_parts.append(raw_part)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, "&".join(redacted_parts), parts.fragment)
    )


def _response_error_message(response: Any) -> str:
    if response is None:
        return ""
    try:
        payload = response.json()
    except ValueError:
        text = str(getattr(response, "text", "")).strip()
        return redact_credentials_in_text(text[:200])
    if isinstance(payload, Mapping):
        result = payload.get("result")
        if isinstance(result, Mapping) and result.get("message"):
            return redact_credentials_in_text(str(result["message"]))
        response_body = payload.get("response")
        if isinstance(response_body, Mapping):
            header = response_body.get("header")
            if isinstance(header, Mapping) and header.get("resultMsg"):
                return redact_credentials_in_text(str(header["resultMsg"]))
    return ""


def _validate_apihub_base_url(base_url: str) -> str:
    clean = base_url.rstrip("/")
    parts = urlsplit(clean)
    if parts.scheme != "https" or parts.hostname not in _APIHUB_ALLOWED_HOSTS:
        raise ValueError(f"base_url must be https://apihub.kma.go.kr, got {base_url!r}")
    return clean


def _charset_from_content_type(content_type: str) -> str:
    match = re.search(r"charset=([^\s;]+)", content_type, re.I)
    if not match:
        return "utf-8"
    return match.group(1).strip("\"'")


def _check_apihub_result_code(response: ApiHubResponse, *, endpoint: str) -> None:
    try:
        payload = json.loads(response.text)
    except ValueError:
        raise_for_kma_xml_error_body(
            response.text,
            provider="apihub",
            endpoint=endpoint,
            label="APIHub",
        )
        return
    if not isinstance(payload, Mapping):
        return
    envelope = payload.get("response")
    if not isinstance(envelope, Mapping):
        return
    header = envelope.get("header")
    if not isinstance(header, Mapping):
        return
    code = str(header.get("resultCode", ""))
    if not code or code in ("00", NO_DATA_RESULT_CODE):
        return
    raise_for_kma_result_code(
        code,
        str(header.get("resultMsg", "")),
        provider="apihub",
        endpoint=endpoint,
        label="APIHub",
    )


def _debug_parse_apihub_response(response: ApiHubResponse, response_kind: str) -> tuple[Any, Any]:
    """`debug_fetch_endpoint`용으로 `response_kind`에 맞춰 raw/processed 값을 만듭니다.

    반환값은 `(parsed, processed)`입니다. `processed`는 list 모양이면 Streamlit
    쪽에서 dataframe으로, 아니면 단일 object로 표시됩니다.
    """

    if response_kind == "structured":
        try:
            parsed = response.json()
        except KmaParseError:
            parsed = {"text_preview": response.text[:2000]}
        return parsed, parsed
    if response_kind == "text":
        table = response.text_table()
        rows = [dict(row) for row in table.rows]
        parsed = {
            "headers": list(table.headers),
            "rows": rows,
            "comments": list(table.comments),
        }
        return parsed, (rows if rows else parsed)
    if response_kind == "image":
        image = response.image()
        parsed = {
            "content_type": image.content_type,
            "format": image.format,
            "width": image.width,
            "height": image.height,
            "bytes": len(image.content),
        }
        return parsed, parsed
    # "file" 또는 알 수 없는 response_kind — 내용을 해석하지 않고 크기/타입만 보여준다.
    parsed = {"content_type": response.content_type, "bytes": len(response.content)}
    return parsed, parsed


def _apihub_open_api_body(response: ApiHubResponse, *, endpoint: str) -> Mapping[str, Any]:
    payload = response.json()
    try:
        body = payload["response"]["body"]
    except (KeyError, TypeError) as exc:
        raise KmaParseError(
            "APIHub response was not in the expected response/header/body shape",
            provider="apihub",
            endpoint=endpoint,
            status_code=response.status_code,
            failure_kind="parse",
            retryable=False,
        ) from exc
    if not isinstance(body, Mapping):
        raise KmaParseError(
            "APIHub response body was not an object",
            provider="apihub",
            endpoint=endpoint,
            status_code=response.status_code,
            failure_kind="parse",
            retryable=False,
        )
    return body


def _normalize_apihub_path(path: str) -> str:
    parts = urlsplit(path)
    clean = parts.path if parts.scheme or parts.netloc else path
    if not clean.startswith("/api/"):
        raise ValueError("APIHub path must start with /api/")
    return clean


def _parse_query_parts(query: str) -> tuple[tuple[str, str], ...]:
    parts: list[tuple[str, str]] = []
    bare_index = 1
    for raw_part in query.split("&"):
        if raw_part == "":
            continue
        if "=" in raw_part:
            key, _value = raw_part.split("=", 1)
            key = unquote_plus(key)
            if key == "authKey":
                continue
            parts.append(("named", key))
        else:
            parts.append(("bare", f"arg{bare_index}"))
            bare_index += 1
    return tuple(parts)


def _query_part_value(query: str, kind: str, name: str) -> str:
    bare_index = 1
    for raw_part in query.split("&"):
        if raw_part == "":
            continue
        if "=" in raw_part:
            key, value = raw_part.split("=", 1)
            if kind == "named" and unquote_plus(key) == name:
                return unquote_plus(value)
        else:
            current = f"arg{bare_index}"
            if kind == "bare" and current == name:
                return unquote_plus(raw_part)
            bare_index += 1
    return ""


def _quote_query_value(value: Any) -> str:
    return quote_plus(str(value), safe=",.:/")


def _guess_text_headers(comments: tuple[str, ...], data_lines: list[str]) -> tuple[str, ...]:
    for comment in reversed(comments):
        candidate = comment.lstrip("#").strip()
        if not candidate:
            continue
        fields = candidate.replace(",", " ").split()
        if len(fields) >= 2 and _looks_like_header(fields):
            return tuple(fields)
    if len(data_lines) >= 2:
        fields = data_lines[0].split()
        values = data_lines[1].split()
        if len(fields) >= 2 and len(values) >= len(fields) and _looks_like_header(fields):
            del data_lines[0]
            return tuple(fields)
    return ()


def _looks_like_header(fields: list[str]) -> bool:
    return any(re.search(r"[A-Za-z_가-힣]", field) for field in fields)


def _detect_jpeg_size(content: bytes) -> tuple[int, int] | None:
    index = 2
    while index + 9 < len(content):
        if content[index] != 0xFF:
            index += 1
            continue
        marker = content[index + 1]
        index += 2
        if marker in {0xD8, 0xD9}:
            continue
        if index + 2 > len(content):
            return None
        length = int.from_bytes(content[index : index + 2], "big")
        if length < 2 or index + length > len(content):
            return None
        if 0xC0 <= marker <= 0xC3:
            height = int.from_bytes(content[index + 3 : index + 5], "big")
            width = int.from_bytes(content[index + 5 : index + 7], "big")
            return width, height
        index += length
    return None
