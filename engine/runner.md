# runner.py 동작 상세

단일 전략 실행 프로세스. watchdog.py가 전략별로 독립 실행시킨다.

---

## 시작 ~ 루프 진입

```
watchdog.py
  └─ python runner.py <strategy_id>
          │
          ▼
       run(strategy_id)
          │
    ┌─────┴──────────────────────────────────────┐
    │ 1. 전략 JSON 로드                           │
    │ 2. 거래일 확인 → 비거래일이면 종료          │
    │ 3. KISApi 인스턴스 생성                     │
    │ 4. 예수금 조회 → 종목당 투자금 = 예수금×10% │
    │ 5. while True 루프 진입                     │
    └────────────────────────────────────────────┘
```

---

## 루프 흐름

```
while True
    │
    ├─ t < 855  →  자정 초기화 (entry_done 리셋)
    │
    ├─ t >= 1520, 오늘 강제매도 안 했으면  →  force_sell_strategy()
    │
    ├─ t >= 1530, 오늘 결산 안 했으면  →  send_strategy_summary() → return (종료)
    │
    ├─ t >= 1520  →  sleep(30) → continue
    │
    ├─ 매수 블록 (전략 타입에 따라 분기)
    │   ├─ [일반] entry_t <= t < entry_t+10, entry_done=False
    │   │     → KODEX200 3분봉 체크
    │   │          하락: entry_done=True, 당일 종료
    │   │          상승: execute_entry() → entry_done=True
    │   │
    │   └─ [연속] entry_t <= t < entry_end_t, 내 포지션 없음
    │         → execute_entry()  (KODEX200 체크는 내부에서 종목별로 실행)
    │
    ├─ 900 <= t < 1520  →  check_and_exit()  (포지션 모니터링)
    │
    └─ sleep(monitor_interval_sec)
```

---

## execute_entry() 상세

```
screen(api, strategy)
    │
종목 없음 → notify_no_signal() → return
    │
종목 있음, 종목별 반복:
    │
    ├─ reserve_or_skip(code, sid)
    │     타 전략 보유/예약 중 → 스킵
    │
    ├─ 잔고 확인
    │     부족 → 경고 + 스킵
    │
    ├─ 체결강도 재확인 (min_execution_strength 설정된 전략만)
    │     기준 미달 → cancel_reservation() + 스킵
    │
    ├─ KODEX200 3분봉 상승 확인
    │     하락 → cancel_reservation() + 스킵
    │
    └─ api.buy() 시장가 매수
          성공 → notify_buy() + add_position()
          실패 → 최대 3회 재시도 (1초, 2초 간격)
                  500 에러 후 실제 체결된 경우 잔고 확인으로 감지
                  최종 실패 → notify_error() + cancel_reservation()
```

---

## force_sell_strategy() 상세

15:20에 자신의 전략 포지션만 전량 강제매도.

- 최대 5회 재시도 (3초 간격) — 장마감 시간대 API 연결 불안정 대응
- "잔고내역이 없습니다" 에러 → 이미 매도된 것으로 간주, 포지션만 제거
- try/finally 구조로 매도 완료 후 반드시 remove_position() 실행

---

## 상태 변수

| 변수 | 역할 |
|------|------|
| `entry_done` | 일반 전략: 오늘 매수 실행 여부 |
| `force_sold_day` | 장마감 강제매도 완료 날짜 (중복 방지) |
| `summary_sent_day` | 결산 발송 완료 날짜 (중복 방지) |

---

## 종료 처리

| 원인 | 처리 |
|------|------|
| 15:30 결산 완료 | 정상 return |
| 비거래일 | 텔레그램 알림 후 return |
| KeyboardInterrupt | 로그만 남기고 종료 |
| 예상치 못한 예외 | traceback 로그 + 텔레그램 오류 알림 |

> watchdog.py가 비정상 종료를 감지하면 자동 재시작한다.
