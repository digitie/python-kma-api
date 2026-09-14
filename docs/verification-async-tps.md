# 비동기 전용 및 공통 TPS 검증

2026-09-14, 최신 main `1f8d8df137db2c57e0fea99b730bf4976c8fbfe0` 기준 변경이다.

- 오프라인: 178 passed, 14 subtests passed. 실제 API 12개는 별도 opt-in이다.
- ruff 전체, mypy 25개 소스 파일, compileall 통과.
- 기존 공개 메서드 인자·기본값과 APIHub endpoint 470개/첨부 77개 metadata를 유지했다.
- 생성기로 만든 470개 endpoint를 MockTransport로 전부 await 호출하고 송신별 토큰을 확인했다.
- 독립 리뷰 A: 17개 별도 회귀 검증 및 전체 오프라인 재실행 통과, 최종 승인.
  예보 파싱 오류, APIHub resultCode 오류, 사용자 지정 인증 이름 override의 키 보호를 보강했다.
- 독립 리뷰 B: 도메인 모의 요청 20개, 생성 endpoint 470개, 관련 테스트 95개,
  문서 예제 56개 실행 통과, 최종 승인. 문서의 누락 await·세션 생성·종료와 필수 인자를 수정했다.

두 최종 리뷰 이후 tests/test_live_services.py의 실제 API 테스트를 실행했다.
결과는 **11 passed, 1 skipped**다. 유일한 skip은 정상 데이터 응답 성공이 아니다.

| API | 실제 결과 | 판단 |
|---|---|---|
| AsosHourlyInfoService/getWthrDataList (종관기상관측 시간자료) | HTTP 403 | 서비스키 권한 거부로 skip, 사용자에게 보고 |
| 나머지 11개 live 테스트 | 통과 | 해당 테스트의 응답·타입 계약 확인 |

이번 실행에는 NO_DATA로 인한 skip이 없었다. 403을 허용하는 사용자 지시가 아직 없으므로
이 PR은 **머지 보류**한다. 기존에 승인된 KHOA·datagokr·안산세계음식점·산불통계 예외를
이 서비스의 권한 오류로 확대하지 않았다. API 전체 470개의 실서버 성공을 주장하지 않는다.

변경 계약과 호출 예제는 [비동기 API와 공통 TPS](async-tps.md)를 참고한다.
