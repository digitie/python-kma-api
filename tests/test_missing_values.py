"""KMA Missing sentinels (|v| >= 900) in 단기예보 observation/forecast values.

The 단기예보 조회서비스 활용가이드 says a value of +900 or more / -900 or less is
"Missing" (no instrument / no observation).  Production getUltraSrtNcst answered a
station with no observation with ``REH/VEC -998``, ``RN1/WSD/UUU/VVV -998.9`` and
``T1H -999`` -- the payloads below are that shape.
"""

from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest

import kma
from kma import is_missing
from kma.client import KmaClient
from kma.codes import normalize_value
from kma.enums import enum_value
from kma.time_utils import KST
from kma.timeline import pivot_forecast_items


class _Response:
    status_code = 200

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _Session:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    async def get(self, url: str, *, params: dict[str, Any], timeout: float) -> _Response:
        del url, params, timeout
        return _Response(self.payload)


def _payload(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "response": {
            "header": {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"},
            "body": {
                "dataType": "JSON",
                "pageNo": 1,
                "numOfRows": 1000,
                "totalCount": len(items),
                "items": {"item": items},
            },
        }
    }


def _ncst(category: str, value: str) -> dict[str, Any]:
    return {
        "baseDate": "20261005",
        "baseTime": "1400",
        "category": category,
        "nx": 60,
        "ny": 127,
        "obsrValue": value,
    }


def _fcst(category: str, value: str) -> dict[str, Any]:
    return {
        "baseDate": "20261005",
        "baseTime": "1400",
        "category": category,
        "fcstDate": "20261005",
        "fcstTime": "1500",
        "fcstValue": value,
        "nx": 60,
        "ny": 127,
    }


#: The exact sentinels production answered with for a station with no observation.
_PROD_SENTINEL_NCST = [
    _ncst("PTY", "0"),
    _ncst("REH", "-998"),
    _ncst("RN1", "-998.9"),
    _ncst("T1H", "-999"),
    _ncst("UUU", "-998.9"),
    _ncst("VEC", "-998"),
    _ncst("VVV", "-998.9"),
    _ncst("WSD", "-998.9"),
]

_WHEN = datetime(2026, 10, 5, 14, 45, tzinfo=KST)


async def test_now_reports_missing_sentinels_as_none_not_as_measurements() -> None:
    client = KmaClient("decoded-key", session=_Session(_payload(_PROD_SENTINEL_NCST)))

    snapshot = await client.now(nx=60, ny=127, when=_WHEN)

    assert snapshot.temperature is None
    assert snapshot.humidity is None
    assert snapshot.wind_speed is None
    assert snapshot.wind_direction is None
    assert snapshot.precipitation is None
    # The raw strings stay available for callers that want to see the sentinel.
    assert snapshot.raw["by_category"]["T1H"] == "-999"
    assert snapshot.raw["by_category"]["RN1"] == "-998.9"
    # PTY was observed ("0" = 없음) and is unaffected.
    assert snapshot.precipitation_label == "없음"


async def test_now_keeps_real_values_next_to_a_missing_one() -> None:
    items = [
        _ncst("T1H", "18.4"),
        _ncst("REH", "-998"),
        _ncst("WSD", "3.1"),
        _ncst("VEC", "359"),
        _ncst("RN1", "강수없음"),
        _ncst("PTY", "0"),
    ]
    client = KmaClient("decoded-key", session=_Session(_payload(items)))

    snapshot = await client.now(nx=60, ny=127, when=_WHEN)

    assert snapshot.temperature == 18.4
    assert snapshot.humidity is None
    assert snapshot.wind_speed == 3.1
    assert snapshot.wind_direction == 359
    assert snapshot.precipitation == 0.0


@pytest.mark.parametrize(
    ("value", "expected"),
    [("-899.9", -899.9), ("899.9", 899.9), ("-40.5", -40.5), ("0", 0.0)],
)
async def test_now_keeps_values_just_inside_the_threshold(value: str, expected: float) -> None:
    client = KmaClient("decoded-key", session=_Session(_payload([_ncst("T1H", value)])))

    snapshot = await client.now(nx=60, ny=127, when=_WHEN)

    assert snapshot.temperature == expected


async def test_forecast_reports_missing_sentinels_as_none() -> None:
    items = [
        _fcst("TMP", "-999"),
        _fcst("REH", "-998"),
        _fcst("WSD", "-998.9"),
        _fcst("POP", "900"),
        _fcst("PCP", "-998.9"),
        _fcst("SKY", "-998"),
        _fcst("TMN", "12.0"),
    ]
    client = KmaClient("decoded-key", session=_Session(_payload(items)))

    forecast = await client.forecast(nx=60, ny=127, when=_WHEN)

    values = {enum_value(item.category): item.value for item in forecast}
    assert values == {
        "TMP": None,
        "REH": None,
        "WSD": None,
        "POP": None,
        "PCP": None,
        "SKY": None,
        "TMN": 12.0,
    }
    assert {enum_value(item.category): item.raw["fcstValue"] for item in forecast}["TMP"] == "-999"
    timepoint = pivot_forecast_items(forecast)[0]
    assert timepoint.value("TMP") is None
    assert timepoint.value("TMN") == 12.0


def test_normalize_value_treats_sentinels_as_missing_but_keeps_labels() -> None:
    assert normalize_value("T1H", "-999") is None
    assert normalize_value("UUU", "-998.9") is None
    assert normalize_value("PCP", "-998.9") is None
    assert normalize_value("TMP", " ") is None
    assert normalize_value("PCP", "1.0mm 미만") == "1.0mm 미만"
    assert normalize_value("PCP", "50.0mm 이상") == "50.0mm 이상"
    assert normalize_value("TMP", "-12.3") == -12.3


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        "\t\n",
        "-998",
        "-998.9",
        "-999",
        " -999 ",
        "900",
        "-900",
        "+900",
        "999.0",
        "9e2",
        "1e400",
        -998.9,
        -999,
        900.0,
        Decimal("-998.9"),
        Decimal("900"),
    ],
)
def test_is_missing_true(value: object) -> None:
    assert is_missing(value) is True  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    [
        "0",
        "0.0",
        "-899.9",
        "899.99",
        "18.4",
        "강수없음",
        "1.0mm 미만",
        "30.0~50.0mm",
        "50.0mm 이상",
        "abc",
        "nan",
        "NaN",
        "inf",
        "-Infinity",
        "sNaN",
        0,
        -899.9,
        math.nan,
        math.inf,
        -math.inf,
        Decimal("NaN"),
        Decimal("-Infinity"),
        Decimal("899.9"),
    ],
)
def test_is_missing_false(value: object) -> None:
    # NaN/Infinity are not KMA sentinels; whether they are *invalid* is the
    # caller's decision.  Non-numeric labels are values, not missing.
    assert is_missing(value) is False  # type: ignore[arg-type]


def test_is_missing_is_public_and_documented() -> None:
    assert "is_missing" in kma.__all__
    assert kma.is_missing is is_missing
    assert is_missing.__doc__ and "900" in is_missing.__doc__
    assert kma.KMA_MISSING_ABS_THRESHOLD == Decimal("900")
