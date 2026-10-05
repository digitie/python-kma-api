"""KMA 단기예보 Missing 센티널 판정.

기상청 단기예보 조회서비스(초단기실황 ``getUltraSrtNcst``, 초단기예보
``getUltraSrtFcst``, 단기예보 ``getVilageFcst``) 활용가이드는 관측·예보값이
``+900`` 이상이거나 ``-900`` 이하이면 **Missing**(관측장비 없음·결측)이라고
정의한다. 실제 운영 응답에서는 관측이 없는 격자에 대해 ``REH/VEC -998``,
``RN1/WSD/UUU/VVV -998.9``, ``T1H -999``가 왔다.

이 규칙은 위 단기예보 계열의 ``obsrValue``/``fcstValue``에만 적용된다. 기압(hPa),
조위(cm), 격자·지점 번호처럼 정상값이 900을 넘을 수 있는 필드에는 쓰지 않는다.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

__all__ = ["KMA_MISSING_ABS_THRESHOLD", "is_missing"]

#: 절댓값이 이 값 이상이면 KMA Missing 센티널이다(활용가이드 "+900 이상, -900 이하").
KMA_MISSING_ABS_THRESHOLD = Decimal("900")


def is_missing(value: str | float | Decimal | None) -> bool:
    """KMA 단기예보 관측·예보값이 "값 없음"인지 판정합니다.

    다음이면 ``True``:

    - ``None``, 빈 문자열, 공백만 있는 문자열
    - 유한한 숫자(또는 숫자 문자열)로서 ``abs(value) >= 900`` -- 활용가이드의
      Missing 센티널(예: ``"-998"``, ``"-998.9"``, ``"-999"``, ``900``).
      문자열은 ``Decimal``로 읽으므로 ``"1e400"``처럼 float로는 넘치는 값도
      센티널로 본다.

    다음은 ``False``:

    - 범위 안의 숫자(``"-899.9"``, ``"18.4"``, ``0``)
    - 숫자가 아닌 라벨(``"강수없음"``, ``"1.0mm 미만"``, ``"50.0mm 이상"``) --
      값이 있는 것이며 해석은 호출자 몫이다.
    - ``NaN``/``Infinity`` -- 센티널이 아니다. 유효하지 않은 값으로 다룰지는
      호출자가 정한다.

    단기예보 계열(``getUltraSrtNcst``/``getUltraSrtFcst``/``getVilageFcst``와 같은
    category 체계를 쓰는 해수욕장 예보)의 ``obsrValue``/``fcstValue``에만 쓴다.
    """

    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, (int, float)):
        number = Decimal(value)
    else:
        text = str(value).strip()
        if not text:
            return True
        try:
            number = Decimal(text)
        except (InvalidOperation, ValueError):
            return False
    return number.is_finite() and abs(number) >= KMA_MISSING_ABS_THRESHOLD
