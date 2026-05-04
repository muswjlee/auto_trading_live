# runner.py 동작 상세 설명

메인 루프 파일. 프로그램이 시작되면 이 파일 하나가 하루 종일 돌면서 모든 동작을 처리한다.

---

## 시작 ~ 루프 진입까지

```
python -m engine.runner
         │
         ▼
    run() 호출
         │
    ┌────┴────────────────────────────────────┐
    │ 1. KISApi 인스턴스 생성                  │
    │ 2. 활성 전략 JSON 로드 (enabled: true)   │
    │ 3. 전략 없으면 텔레그램 알림 후 종료      │
    │ 4. 시작 메시지 텔레그램 발송             │
    │ 5. 거래일 확인 → 비거래일이면 종료       │
    │ 6. while True 루프 진입                  │
    └─────────────────────────────────────────┘
```

**거래일 확인 순서 (`is_trading_day`):**
1. 주말(토/일)이면 바로 False
2. KIS `chk-holiday` API 조회 → 모의투자 서버는 미지원으로 실패
3. `holidays` 라이브러리로 한국 공휴일 확인
4. 5월 1일(근로자의 날)은 라이브러리 누락이라 수동 처리
5. 모든 확인 실패 시 거래일로 간주

---

## 루프 내 시간 판단 방식

매 루프마다 현재 시각을 정수로 변환해 비교한다.

```python
t = now.hour * 100 + now.minute
# 09:10 → 910
# 15:20 → 1520
# 10:05 → 1005
```

루프 순서도:

```
while True
    │
    ├─ t < 900  →  자정 초기화 (entry_done 클리어)
    │
    ├─ t >= 1520, 오늘 강제매도 안 했으면  →  15:20 강제매도
    │       └─ 완료 후 sleep(60) → continue (루프 처음으로)
    │
    ├─ t >= 1530, 오늘 결산 안 했으면  →  15:30 결산 요약 발송
    │
    ├─ t >= 1520  →  sleep(60) → continue  (장 마감 후 대기)
    │
    ├─ 전략별 매수 체크 (entry_time window 진입 여부)
    │
    ├─ 910 <= t < 1520  →  포지션 모니터링
    │
    └─ sleep(30초)  →  다음 루프
```

---

## 구간별 상세 동작

### 자정 초기화 (`t < 900`)

```python
if t < 900:
    if force_sold_day != today:
        entry_done.clear()
```

프로그램이 자정을 넘겨 다음날까지 실행될 경우를 대비한 처리.  
`force_sold_day`가 어제 날짜이면 `today`와 달라지므로 `entry_done`을 비워서 다음날 매수가 가능하게 한다.

> 실제로는 당일 9:00에 시작해서 15:30 이후 대기 중에 자정을 넘기는 경우에만 동작한다.

---

### 15:20 강제 장마감 매도

```python
if t >= 1520 and force_sold_day != today:
```

`force_sold_day != today` 조건으로 하루에 딱 한 번만 실행.

**동작 순서:**
1. `positions.json` 로드
2. 포지션 있으면 → 종목별 현재가 조회 후 시장가 매도
3. 매도 완료 시: 텔레그램 알림 → `daily_pnl.json` 기록 → 포지션 제거
4. 개별 종목 오류 발생 시: 에러 로그 + 텔레그램 알림, **다른 종목은 계속 진행**
5. `force_sold_day = today` 설정 (중복 실행 방지)
6. `sleep(60)` 후 `continue` → 루프 처음으로 돌아감

> `continue`로 인해 아래 결산/모니터링 코드는 이 루프에서 실행되지 않는다.  
> 15:30이 되면 다음 루프에서 결산 블록이 실행된다.

---

### 15:30 일일 결산 요약

```python
if t >= 1530 and summary_sent_day != today:
    try:
        ...
        summary_sent_day = today
    except Exception as e:
        log.error(...)
        notify_error(...)
```

전체를 `try/except`로 감싸서 `get_balance()` 실패 시에도 프로그램이 계속 실행된다.  
실패하면 `summary_sent_day`가 설정되지 않으므로 다음 루프(60초 후)에서 재시도한다.

**결산 메시지 구성:**
1. `daily_pnl.json`에서 당일 거래 내역 로드
2. `get_balance()`로 현재 예수금·총평가금액 조회
3. 거래별 손익(수익 ✅ / 손실 🔴) 목록
4. 당일 실현손익 합계
5. 총평가금액, 예수금

---

### 장 마감 후 대기

```python
if t >= 1520:
    time.sleep(60)
    continue
```

강제매도·결산이 완료된 이후에는 아무것도 하지 않고 60초마다 대기.  
프로그램을 수동 종료(`Ctrl+C`)하기 전까지 이 상태를 유지한다.

---

### 전략별 매수 실행

```python
for strategy in strategies:
    if strategy["id"] in entry_done:
        continue
    h, m    = map(int, strategy["schedule"]["entry_time"].split(":"))
    entry_t = h * 100 + m
    if entry_t <= t < entry_t + 10:
        execute_entry(api, strategy)
        entry_done.add(strategy["id"])
```

**매수 window:** `entry_time`부터 10분간  
예: `entry_time = "09:10"` → `910 <= t < 920` 구간에서만 실행

**`entry_done` set:** 한 번 매수한 전략 ID를 기록해서 같은 전략이 다음 루프에서 중복 실행되지 않도록 막는다.

| 전략 | entry_time | window |
|------|-----------|--------|
| vol_surge | 09:10 | 910 ~ 919 |
| value_buffett | 10:00 | 1000 ~ 1009 |
| rsi_macd | 10:30 | 1030 ~ 1039 |

---

### `execute_entry()` — 매수 실행 상세

```
screen(api, strategy)
        │
   종목 없음  →  notify_no_signal() → return
        │
   종목 있음
        │
   종목별 반복:
        ├─ 투자금 ÷ 현재가 = 매수 수량 (최소 1주)
        ├─ 잔고 확인 → 부족하면 skip
        └─ api.buy() 시도
                ├─ 성공 → 로그 + 텔레그램 + 포지션 등록
                └─ 실패 → 재시도 (최대 3회)
                          1차 실패: 1초 대기 후 재시도
                          2차 실패: 2초 대기 후 재시도
                          3차 실패: 에러 로그 + 텔레그램 알림
```

**수량 계산:**
```python
qty = max(1, amount_per_stock // price)
# 100만원 / 68,000원 = 14주
# 100만원 / 2,000원 = 500주
```

---

### 포지션 모니터링

```python
if 910 <= t < 1520:
    check_and_exit(api, strategies)
```

9:10 ~ 15:20 구간에서만 실행.  
`check_and_exit`는 포지션의 `strategy_id`로 해당 전략의 exit 조건(익절/손절 %)을 찾아 적용한다.  
전략 수와 무관하게 **포지션당 API 호출 1회**만 발생.

---

## 상태 변수 정리

| 변수 | 타입 | 역할 |
|------|------|------|
| `entry_done` | `set` | 오늘 이미 매수 실행한 전략 ID 목록. 루프가 돌아도 중복 매수 방지 |
| `force_sold_day` | `date` or `None` | 강제매도 완료한 날짜. 오늘 날짜와 같으면 스킵 |
| `summary_sent_day` | `date` or `None` | 결산 요약 발송한 날짜. 오늘 날짜와 같으면 스킵 |

---

## 종료 처리

| 종료 원인 | 처리 |
|-----------|------|
| `Ctrl+C` (KeyboardInterrupt) | "🛑 자동매매 수동 종료" 텔레그램 발송 |
| 예상치 못한 예외 | traceback 전체 로그 저장 + "🆘 자동매매 오류 종료" 텔레그램 발송 |
| 비거래일 | "📵 비거래일" 텔레그램 발송 후 정상 종료 |
| 전략 없음 | "실행할 전략이 없습니다" 텔레그램 발송 후 정상 종료 |

---

## 전체 하루 타임라인 예시

```
09:00  프로그램 시작, 거래일 확인, 텔레그램 "자동매매 시작" 발송
       └─ while 루프 시작

09:10  vol_surge 스크리닝 + 매수 실행 → entry_done = {"vol_surge"}
09:10~ 30초마다 포지션 모니터링 (익절/손절 체크)

09:45  [예시] 채비 +2.1% 익절 → 매도 → 텔레그램 알림

10:00  value_buffett 스크리닝 + 매수 실행 → entry_done += "value_buffett"
10:30  rsi_macd 스크리닝 + 매수 실행 → entry_done += "rsi_macd"

10:30~ 30초마다 포지션 모니터링 계속

15:20  미청산 포지션 전량 강제매도 → force_sold_day = today
       sleep(60) → continue

15:30  일일 결산 요약 텔레그램 발송 → summary_sent_day = today
15:30~ 60초마다 대기 (아무것도 안 함)

다음날 09:00  작업 스케줄러가 프로그램 재시작
```
