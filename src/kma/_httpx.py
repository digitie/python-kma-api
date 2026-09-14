"""토큰을 얻은 HTTPX 요청의 후속 redirect도 같은 예산으로 전송한다."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from ._ratelimit import AsyncTokenBucket


async def send_after_token(
    client: httpx.AsyncClient,
    request: httpx.Request,
    rate_limiter: AsyncTokenBucket | None,
    *,
    stream: bool = False,
    follow_redirects: bool | None = None,
    before_send: Callable[[], None] | None = None,
) -> httpx.Response:
    """호출자가 첫 토큰을 확보한 요청을 전송하고 redirect마다 추가 과금한다.

    HTTPX가 만든 next_request를 사용해 301/302/303/307/308의 메서드·쿠키·
    인증 헤더 처리를 유지한다. 최종 streaming 응답은 호출자가 닫는다.
    """
    follow = client.follow_redirects if follow_redirects is None else follow_redirects
    history: list[httpx.Response] = []
    while True:
        if before_send is not None:
            before_send()
        if history:
            # next_request에서 HTTPX가 제거한 cross-origin 인증을 재부착하지 않는다.
            response = await client.send(request, stream=stream, follow_redirects=False, auth=None)
        else:
            response = await client.send(request, stream=stream, follow_redirects=False)
        response.history = list(history)
        next_request = response.next_request
        if not follow or next_request is None:
            return response
        try:
            if len(history) >= client.max_redirects:
                raise httpx.TooManyRedirects("Exceeded maximum allowed redirects", request=request)
            # 스트리밍 중간 응답은 본문을 버려 상위 계층의 메모리 상한을 우회하지 않는다.
            if not stream:
                await response.aread()
        finally:
            await response.aclose()
        history.append(response)
        request = next_request
        if rate_limiter is not None:
            await rate_limiter.acquire()
