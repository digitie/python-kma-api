"""기상청 단기예보 공개 API 클라이언트."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from ._credentials import DATA_GOKR_ENV_NAMES, first_env_value, normalize_api_key
from ._http import (
    NO_DATA_RESULT_CODE,
    build_async_client,
    empty_kma_body,
    get_with_retries,
    raise_for_kma_http_error,
    raise_for_kma_network_error,
    raise_for_kma_result_code,
    raise_for_kma_xml_error_body,
    validate_async_session,
)
from ._parsing import float_or_none as _float_or_none
from ._parsing import int_or_none as _int_or_none
from ._ratelimit import AsyncTokenBucket
from ._redact import credential_values, redact_exception
from .codes import label_for, normalize_value, parse_amount
from .enums import KmaEndpoint, WeatherCategory, coerce_category, enum_value
from .exceptions import KmaError, KmaParseError
from .grid import validate_grid
from .locations import LocationInput, normalize_location
from .metadata import ResponseMetadata, make_response_metadata
from .models import ForecastItem, WeatherSnapshot
from .pagination import has_next_page
from .time_utils import (
    as_kst,
    latest_ultra_srt_fcst_base,
    latest_ultra_srt_ncst_base,
    latest_vilage_base,
    parse_kma_datetime,
)

DEFAULT_BASE_URL = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0"
SERVICE_NAME = "VilageFcstInfoService_2.0"


@dataclass(frozen=True)
class _KmaBody:
    body: Mapping[str, Any]
    metadata: ResponseMetadata


@dataclass(frozen=True)
class _FetchedItems:
    items: list[Mapping[str, Any]]
    metadata: ResponseMetadata


class KmaClient:
    """기상청 `VilageFcstInfoService_2.0` endpoint 클라이언트."""

    def __init__(
        self,
        service_key: str,
        *,
        timeout: float = 10,
        retries: int = 3,
        base_url: str | None = None,
        session: Any | None = None,
        max_rps: float = 5.0,
        rate_limiter: AsyncTokenBucket | None = None,
    ) -> None:
        self.service_key = normalize_api_key(service_key, field_name="service_key")
        self.timeout = timeout
        self.retries = retries
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._session = session
        self._owns_session = session is None
        validate_async_session(session)
        self.rate_limiter = rate_limiter if rate_limiter is not None else AsyncTokenBucket(max_rps)
        self.closed = False
        self.forecast: ForecastService = ForecastService(self)

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
    def from_env(cls, name: str = "DATA_GO_KR_SERVICE_KEY", **kwargs: Any) -> KmaClient:
        names = (
            DATA_GOKR_ENV_NAMES
            if name == "DATA_GO_KR_SERVICE_KEY"
            else (name, *DATA_GOKR_ENV_NAMES)
        )
        service_key = first_env_value(names)
        return cls(service_key=service_key, **kwargs)

    async def __aenter__(self) -> KmaClient:
        self._ensure_open()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def now(
        self,
        *,
        location: LocationInput | None = None,
        lat: float | None = None,
        lon: float | None = None,
        nx: int | None = None,
        ny: int | None = None,
        when: datetime | None = None,
    ) -> WeatherSnapshot:
        """Asynchronously fetch `getUltraSrtNcst` observations."""

        grid_x, grid_y = self._coordinates(
            location=location,
            lat=lat,
            lon=lon,
            nx=nx,
            ny=ny,
        )
        base_date, base_time = latest_ultra_srt_ncst_base(when)
        fetched = await self._fetch_items(
            KmaEndpoint.ULTRA_SRT_NCST,
            base_date=base_date,
            base_time=base_time,
            nx=grid_x,
            ny=grid_y,
        )
        items = fetched.items
        by_category = {str(item.get("category")): item.get("obsrValue") for item in items}
        raw = {"items": items, "by_category": by_category}

        return WeatherSnapshot(
            observed_at=parse_kma_datetime(base_date, base_time),
            nx=grid_x,
            ny=grid_y,
            temperature=_float_or_none(by_category.get(WeatherCategory.CURRENT_TEMPERATURE.value)),
            humidity=_int_or_none(by_category.get(WeatherCategory.HUMIDITY.value)),
            wind_speed=_float_or_none(by_category.get(WeatherCategory.WIND_SPEED.value)),
            wind_direction=_int_or_none(by_category.get(WeatherCategory.WIND_DIRECTION.value)),
            precipitation=parse_amount(by_category.get(WeatherCategory.ONE_HOUR_RAIN.value)),
            sky_label=label_for(
                WeatherCategory.SKY,
                by_category.get(WeatherCategory.SKY.value),
                endpoint=KmaEndpoint.ULTRA_SRT_NCST,
            ),
            precipitation_label=label_for(
                WeatherCategory.PRECIPITATION_TYPE,
                by_category.get(WeatherCategory.PRECIPITATION_TYPE.value),
                endpoint=KmaEndpoint.ULTRA_SRT_NCST,
            ),
            raw=raw,
            metadata=fetched.metadata,
        )

    async def forecast_short(
        self,
        *,
        location: LocationInput | None = None,
        lat: float | None = None,
        lon: float | None = None,
        nx: int | None = None,
        ny: int | None = None,
        when: datetime | None = None,
    ) -> list[ForecastItem]:
        """Asynchronously fetch `getUltraSrtFcst` forecast items."""
        try:
            grid_x, grid_y = self._coordinates(
                location=location,
                lat=lat,
                lon=lon,
                nx=nx,
                ny=ny,
            )
            base_date, base_time = latest_ultra_srt_fcst_base(when)
            fetched = await self._fetch_items(
                KmaEndpoint.ULTRA_SRT_FCST,
                base_date=base_date,
                base_time=base_time,
                nx=grid_x,
                ny=grid_y,
            )
            return [
                _forecast_item(item, KmaEndpoint.ULTRA_SRT_FCST, metadata=fetched.metadata)
                for item in fetched.items
            ]
        except KmaError as exc:
            redact_exception(exc, self.service_key, *credential_values(None))
            raise exc from None

    async def _forecast_vilage(
        self,
        *,
        location: LocationInput | None = None,
        lat: float | None = None,
        lon: float | None = None,
        nx: int | None = None,
        ny: int | None = None,
        when: datetime | None = None,
    ) -> list[ForecastItem]:
        """Asynchronously fetch `getVilageFcst` forecast items."""
        try:
            grid_x, grid_y = self._coordinates(
                location=location,
                lat=lat,
                lon=lon,
                nx=nx,
                ny=ny,
            )
            base_date, base_time = latest_vilage_base(when)
            fetched = await self._fetch_items(
                KmaEndpoint.VILAGE_FCST,
                base_date=base_date,
                base_time=base_time,
                nx=grid_x,
                ny=grid_y,
            )
            return [
                _forecast_item(item, KmaEndpoint.VILAGE_FCST, metadata=fetched.metadata)
                for item in fetched.items
            ]
        except KmaError as exc:
            redact_exception(exc, self.service_key, *credential_values(None))
            raise exc from None

    async def version(self, ftype: str, when: datetime) -> Mapping[str, Any]:
        """Asynchronously fetch `getFcstVersion` metadata."""

        when_kst = as_kst(when)
        base_date = when_kst.strftime("%Y%m%d")
        base_time = when_kst.strftime("%H%M")
        return await self._request(
            KmaEndpoint.FCST_VERSION,
            {
                "ftype": ftype,
                "basedatetime": f"{base_date}{base_time}",
            },
        )

    def _coordinates(
        self,
        *,
        location: LocationInput | None,
        lat: float | None,
        lon: float | None,
        nx: int | None,
        ny: int | None,
    ) -> tuple[int, int]:
        grid = normalize_location(location, lat=lat, lon=lon, nx=nx, ny=ny)
        return grid.nx, grid.ny

    async def _fetch_items(
        self,
        endpoint: str | KmaEndpoint,
        *,
        base_date: str,
        base_time: str,
        nx: int,
        ny: int,
    ) -> _FetchedItems:
        response = await self._request_with_metadata(
            endpoint,
            {
                "base_date": base_date,
                "base_time": base_time,
                "nx": nx,
                "ny": ny,
            },
        )
        if has_next_page(response.body):
            raise KmaParseError(
                "KMA response has more items than the requested page size",
                provider="data.go.kr",
                endpoint=enum_value(endpoint),
                failure_kind="parse",
                retryable=False,
            )
        try:
            items = response.body["items"]["item"]
        except (KeyError, TypeError) as exc:
            raise KmaParseError(
                "KMA response did not contain items.item",
                provider="data.go.kr",
                endpoint=enum_value(endpoint),
                failure_kind="parse",
                retryable=False,
            ) from exc
        if isinstance(items, Mapping):
            return _FetchedItems([items], response.metadata)
        if not isinstance(items, list):
            raise KmaParseError(
                "KMA response items.item was not a list",
                provider="data.go.kr",
                endpoint=enum_value(endpoint),
                failure_kind="parse",
                retryable=False,
            )
        return _FetchedItems(items, response.metadata)

    async def _request(
        self,
        endpoint: str | KmaEndpoint,
        params: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return (await self._request_with_metadata(endpoint, params)).body

    async def _request_with_metadata(
        self,
        endpoint: str | KmaEndpoint,
        params: Mapping[str, Any],
    ) -> _KmaBody:
        try:
            endpoint_name = enum_value(endpoint)
            request_params: dict[str, Any] = {
                "serviceKey": self.service_key,
                "pageNo": 1,
                "numOfRows": 1000,
                "dataType": "JSON",
            }
            request_params.update(params)
            metadata = make_response_metadata(
                provider="data.go.kr",
                service_name=SERVICE_NAME,
                endpoint=endpoint_name,
                request_params=request_params,
                base_date=str(request_params.get("base_date"))
                if request_params.get("base_date") is not None
                else None,
                base_time=str(request_params.get("base_time"))
                if request_params.get("base_time") is not None
                else None,
            )

            try:
                response = await get_with_retries(
                    self._get_session(),
                    f"{self.base_url}/{endpoint_name}",
                    params=request_params,
                    timeout=self.timeout,
                    retries=self.retries,
                    rate_limiter=self.rate_limiter,
                    ensure_open=self._ensure_open,
                )
            except httpx.HTTPStatusError as exc:
                raise_for_kma_http_error(
                    exc,
                    provider="data.go.kr",
                    endpoint=endpoint_name,
                    label="KMA",
                )
            except httpx.RequestError:
                raise_for_kma_network_error(
                    provider="data.go.kr",
                    endpoint=endpoint_name,
                    label="KMA",
                )

            return _parse_kma_body(response, endpoint_name, metadata)
        except KmaError as exc:
            redact_exception(exc, self.service_key, *credential_values(params))
            raise exc from None

    def _get_session(self) -> Any:
        self._ensure_open()
        if self._session is None:
            self._session = build_async_client()
        return self._session


class ForecastService:
    """Async service facade for KMA short-term forecast endpoints."""

    def __init__(self, client: KmaClient) -> None:
        self._client = client

    async def __call__(
        self,
        *,
        location: LocationInput | None = None,
        lat: float | None = None,
        lon: float | None = None,
        nx: int | None = None,
        ny: int | None = None,
        when: datetime | None = None,
    ) -> list[ForecastItem]:
        return await self.vilage(
            location=location,
            lat=lat,
            lon=lon,
            nx=nx,
            ny=ny,
            when=when,
        )

    async def now(
        self,
        *,
        location: LocationInput | None = None,
        lat: float | None = None,
        lon: float | None = None,
        nx: int | None = None,
        ny: int | None = None,
        when: datetime | None = None,
    ) -> WeatherSnapshot:
        return await self._client.now(
            location=location,
            lat=lat,
            lon=lon,
            nx=nx,
            ny=ny,
            when=when,
        )

    async def short(
        self,
        *,
        location: LocationInput | None = None,
        lat: float | None = None,
        lon: float | None = None,
        nx: int | None = None,
        ny: int | None = None,
        when: datetime | None = None,
    ) -> list[ForecastItem]:
        return await self._client.forecast_short(
            location=location,
            lat=lat,
            lon=lon,
            nx=nx,
            ny=ny,
            when=when,
        )

    async def vilage(
        self,
        *,
        location: LocationInput | None = None,
        lat: float | None = None,
        lon: float | None = None,
        nx: int | None = None,
        ny: int | None = None,
        when: datetime | None = None,
    ) -> list[ForecastItem]:
        return await self._client._forecast_vilage(
            location=location,
            lat=lat,
            lon=lon,
            nx=nx,
            ny=ny,
            when=when,
        )

    async def version(self, ftype: str, when: datetime) -> Mapping[str, Any]:
        return await self._client.version(ftype, when)


def _parse_kma_body(response: Any, endpoint_name: str, metadata: ResponseMetadata) -> _KmaBody:
    try:
        payload = response.json()
        envelope = payload["response"]
        header = envelope["header"]
        body = envelope.get("body", {})
    except (ValueError, KeyError, TypeError) as exc:
        if raise_for_kma_xml_error_body(
            str(getattr(response, "text", "")),
            provider="data.go.kr",
            endpoint=endpoint_name,
            label="KMA",
        ):
            return _KmaBody(empty_kma_body(), metadata)
        raise KmaParseError(
            "KMA response was not valid JSON in the expected shape",
            provider="data.go.kr",
            endpoint=endpoint_name,
            status_code=response.status_code,
            failure_kind="parse",
            retryable=False,
        ) from exc

    if not isinstance(header, Mapping):
        raise KmaParseError(
            "KMA response header was not an object",
            provider="data.go.kr",
            endpoint=endpoint_name,
            status_code=response.status_code,
            failure_kind="parse",
            retryable=False,
        )
    code = str(header.get("resultCode", ""))
    message = str(header.get("resultMsg", ""))
    if code == NO_DATA_RESULT_CODE:
        # NO_DATA는 "조회 결과 없음" — 오류가 아니라 정상적인 빈 결과다.
        return _KmaBody(empty_kma_body(body if isinstance(body, Mapping) else None), metadata)
    if code != "00":
        _raise_for_result_code(code, message, endpoint=endpoint_name)
    if not isinstance(body, Mapping):
        raise KmaParseError(
            "KMA response body was not an object",
            provider="data.go.kr",
            endpoint=endpoint_name,
            status_code=response.status_code,
            failure_kind="parse",
            retryable=False,
        )
    return _KmaBody(body, metadata)


def _forecast_item(
    item: Mapping[str, Any],
    endpoint: str | KmaEndpoint,
    *,
    metadata: ResponseMetadata | None = None,
) -> ForecastItem:
    try:
        category = coerce_category(item["category"])
        value = item.get("fcstValue")
        nx = int(item["nx"])
        ny = int(item["ny"])
        validate_grid(nx, ny)
        return ForecastItem(
            base_at=parse_kma_datetime(str(item["baseDate"]), str(item["baseTime"])),
            forecast_at=parse_kma_datetime(str(item["fcstDate"]), str(item["fcstTime"])),
            nx=nx,
            ny=ny,
            category=category,
            value=normalize_value(category, value),
            label=label_for(category, value, endpoint=endpoint),
            raw=dict(item),
            metadata=metadata,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise KmaParseError(
            f"Malformed KMA forecast item: {item!r}",
            provider="data.go.kr",
            endpoint=enum_value(endpoint),
            failure_kind="parse",
            retryable=False,
        ) from exc


def _raise_for_result_code(code: str, message: str, *, endpoint: str) -> None:
    raise_for_kma_result_code(
        code,
        message,
        provider="data.go.kr",
        endpoint=endpoint,
        label="KMA",
    )
