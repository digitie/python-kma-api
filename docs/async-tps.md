# 비동기 API와 공통 TPS

KmaClient, DataGoKrClient, ApiHubClient, ApiHubGeneratedClient는 비동기 전용이다.
네트워크 호출·디버그에는 await, 페이지 순회에는 async for, 종료에는 async with 또는
await client.aclose()를 사용한다. Async 접두사 클래스와 aio 팩터리, a 접두사 메서드는 제거했다.
기존 일반 메서드 이름을 그대로 await한다. ForecastService의 호출과 now/short/vilage/version도 async다.

```python
import asyncio
from kma import AsyncTokenBucket, KmaClient, DataGoKrClient, latest_mid_fcst_time


async def main() -> None:
    budget = AsyncTokenBucket(2, capacity=1)
    async with (
        KmaClient.from_env(rate_limiter=budget) as weather,
        DataGoKrClient.from_env(rate_limiter=budget) as client,
    ):
        print(await weather.now(nx=60, ny=127))
        async for page in (client.iter_pages(
            "MidFcstInfoService", "getMidFcst", {"stnId": "108", "tmFc": latest_mid_fcst_time()}, num_of_rows=10,
        )):
            print(page)


asyncio.run(main())
```

기본 max_rps=5.0, 기본 capacity=max(1, max_rps)는 초기 burst를 허용한다.
일정한 송신 간격은 capacity=1을 지정한다. rate_limiter를 주입하면 max_rps보다 우선하며,
여러 클라이언트가 같은 버킷을 공유하면 합산 TPS가 제한된다. 한 이벤트 루프에서 사용하며,
대기 취소는 토큰을 소비하지 않는다. 공급자의 일일 사용량 제한은 별도다.

일반·타입화 조회, 디버그, 페이지, APIHub 470개 생성 메서드, 탐색 호출은 같은 경로를 쓴다.
첫 송신·재시도·각 리다이렉트마다 한 토큰을 소비한다. 기존 429/500/502/503/504 및 일시적인
연결·timeout 재시도와 jitter/Retry-After를 유지하며, 401/403과 resultCode=22는 즉시 실패한다.
HTTPX의 BasicAuth/기본 인증 흐름을 지원하고 추가 송신을 숨기는 Digest/custom Auth는 거부한다.
사용자 정의 transport 내부의 추가 송신은 해당 transport의 책임이다.

내부 세션은 첫 요청에 만들고 재사용하며 aclose가 닫는다. 주입한 비동기 세션은 호출자가
닫는다. 토큰 대기 중 종료되거나 인증 흐름이 바뀌면 송신 전에 다시 검사한다.
공개 오류와 DebugRun의 모든 필드에서 실제 키와 인코딩된 키를 마스킹한다.

일반 함수로 남는 것은 좌표·시간·코드표·모델·카탈로그·응답 파싱·fixture 저장 등 로컬 작업이다.
JSON/XML NO_DATA03 빈 결과, quota22 비재시도, tmFc 요청값 폴백, PCP/SNO 범주 문자열,
페이지 상한 경고와 APIHub bare query 순서를 유지한다. CLI와 Streamlit은 진입 경계에서만
asyncio.run을 사용하고 종료 시 세션을 닫는다. 생성기 역시 비동기 HTTP와 공통 TPS를 사용한다.
