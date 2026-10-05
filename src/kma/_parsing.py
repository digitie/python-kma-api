"""API 응답 값 변환용 공유 파싱 도우미."""

from __future__ import annotations

import math

from .missing import is_missing


def float_or_none(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def int_or_none(value: object) -> int | None:
    number = float_or_none(value)
    if number is None:
        return None
    return int(number)


def kma_value_or_none(value: object) -> float | None:
    """단기예보 ``obsrValue``/``fcstValue``용 float 변환. Missing 센티널은 ``None``.

    `float_or_none`과 달리 ``abs(value) >= 900``(활용가이드 Missing)을 ``None``으로
    돌린다. 기압·조위·좌표처럼 정상값이 900을 넘는 필드에는 `float_or_none`을 쓴다.
    """

    if isinstance(value, (str, int, float)) and is_missing(value):
        return None
    return float_or_none(value)


def kma_int_or_none(value: object) -> int | None:
    """`kma_value_or_none`의 정수판(습도 ``REH``, 풍향 ``VEC``).

    ``NaN``/``Infinity``는 정수로 바꿀 수 없으므로 예외 대신 ``None``을 반환한다.
    """

    number = kma_value_or_none(value)
    if number is None or not math.isfinite(number):
        return None
    return int(number)


def str_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
