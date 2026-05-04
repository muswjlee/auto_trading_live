# 자동매매 시스템 (KIS API)

한국투자증권 KIS Developers API 기반 자동매매 시스템.  
현재 **모의투자 서버** 운영 중.

---

## 디렉토리 구조

```
auto_trading/
│
├── config.py                  # API 키, 계좌번호, BASE_URL 설정
├── kis_api.py                 # KIS API 래퍼 (토큰·시세·주문·잔고)
├── run_trader.bat             # 작업 스케줄러 실행 진입점
├── requirements.txt           # 패키지 의존성
├── .env                       # API 키 환경변수 (비공개)
├── .env.example               # 환경변수 템플릿
│
├── engine/
│   ├── runner.py              # 메인 루프 (진입·모니터링·마감매도·결산)
│   ├── screener.py            # 종목 스크리닝 (거래량/리서치→등락률→지표 필터)
│   ├── monitor.py             # 포지션 관리, 익절/손절, 손익 기록
│   ├── indicators.py          # RSI, MACD 계산
│   ├── notifier.py            # 텔레그램 알림
│   └── research.py            # 네이버 증권 리서치 보고서 스크래퍼
│
├── strategies/
│   ├── vol_surge.json         # 전략 1: 거래량 급등 + 체결강도
│   ├── value_buffett.json     # 전략 2: 버핏 가치투자
│   ├── rsi_macd.json          # 전략 3: RSI + MACD 골든크로스
│   └── research_morning.json  # 전략 4: 리서치 보고서 급등
│
├── positions.json             # 런타임: 현재 보유 포지션
├── daily_pnl.json             # 런타임: 당일 손익 기록
├── .token_cache.json          # 런타임: KIS 액세스 토큰 캐시
└── trading.log                # 런타임: 실행 로그
```

> `strategy.py`, `trader.py`, `volume_top.py` — 초기 프로토타입 파일, 현재 미사용

---

## 실행 흐름

```
Windows 작업 스케줄러 (월~금 09:00)
  └─ run_trader.bat
       └─ python -m engine.runner
```

| 시각  | 동작 |
|-------|------|
| 09:00 | 프로그램 시작 → 거래일 확인 (비거래일이면 즉시 종료) |
| 09:10 | 전략 1 (vol_surge) 스크리닝 + 매수 |
| 09:15 | 전략 4 (research_morning) 당일 리서치 종목 조회 + 매수 |
| 10:00 | 전략 2 (value_buffett) 스크리닝 + 매수 |
| 10:30 | 전략 3 (rsi_macd) 스크리닝 + 매수 |
| 상시  | 30초마다 포지션 모니터링 (익절 +2% / 손절 -2%) |
| 15:20 | 미청산 포지션 전량 시장가 강제매도 |
| 15:30 | 일일 결산 텔레그램 발송 |

---

## 파일별 상세 설명

---

### `config.py` — 환경 설정

`.env` 파일을 읽어 전역 상수로 제공. 모든 모듈이 이 파일에서 설정값을 임포트.

| 상수 | 설명 |
|------|------|
| `APP_KEY` | KIS API 앱키 |
| `APP_SECRET` | KIS API 앱시크릿 |
| `ACCOUNT_NO` | 계좌번호 앞 8자리 |
| `ACCOUNT_CD` | 계좌 상품코드 (기본 `"01"`) |
| `IS_PAPER` | `True`: 모의투자 / `False`: 실투자 |
| `BASE_URL` | IS_PAPER에 따라 자동 분기되는 API 엔드포인트 |
| `WS_URL` | WebSocket 엔드포인트 (현재 미사용) |

---

### `kis_api.py` — KIS API 래퍼

KIS Developers REST API 전체를 감싸는 클래스. 모든 HTTP 통신은 이 파일에서만 처리.

#### 인증

| 메서드 | 설명 |
|--------|------|
| `_load_token_cache()` | 시작 시 `.token_cache.json`에서 토큰 로드 (재발급 횟수 절감) |
| `_save_token_cache()` | 신규 발급 토큰을 파일에 저장 |
| `_refresh_token()` | KIS OAuth2 토큰 발급 (유효기간 23시간) |
| `token` (property) | 만료 여부 확인 후 유효한 토큰 반환 |
| `_headers(tr_id, extra)` | API 요청 헤더 생성 (Bearer 토큰 포함) |

#### 시세 조회

| 메서드 | 설명 |
|--------|------|
| `get_price(stock_code)` | 현재가·등락률·체결강도·PER·PBR 등 조회 |
| `get_volume_rank(top_n, market, min_price, max_price, min_volume)` | 거래량 상위 종목 조회. API 1회 한계(30개)를 코스피/코스닥/ETF 분리 호출로 우회 후 합산 정렬 |
| `_fetch_volume_rank_page(market, ...)` | 단일 시장 거래량 30개 조회 (내부용) |
| `get_ohlcv(stock_code, period, count)` | 일(D)/주(W)/월(M) OHLCV 조회 |
| `is_trading_day(date)` | 거래일 확인. KIS API 실패 시 `holidays` 라이브러리로 fallback, 근로자의 날(5/1) 수동 처리 |

#### 잔고 조회

| 메서드 | 설명 |
|--------|------|
| `get_balance()` | 보유 종목(`output1`)·총평가액(`output2`) 조회. 500 에러 시 최대 3회 재시도 (1초, 2초 간격) |
| `get_cash()` | `get_balance()`에서 예수금(`dnca_tot_amt`)만 추출해 `int` 반환 |

#### 주문

| 메서드 | 설명 |
|--------|------|
| `_order(stock_code, qty, price, side)` | 매수/매도 공통 주문 처리. `price=0` 이면 시장가(`ORD_DVSN=01`), 아니면 지정가(`00`) |
| `buy(stock_code, qty, price=0)` | 매수 주문 |
| `sell(stock_code, qty, price=0)` | 매도 주문 |
| `get_pending_orders()` | 미체결 주문 목록 조회 |
| `cancel_order(order_no, stock_code, qty)` | 주문 취소 |

---

### `engine/runner.py` — 메인 루프

프로그램의 진입점. `while True` 루프로 시간대별 동작을 순차 처리.

#### 함수

| 함수 | 설명 |
|------|------|
| `load_strategy(strategy_id)` | 특정 전략 JSON 파일 로드 |
| `load_all_enabled_strategies()` | `strategies/` 폴더에서 `enabled: true`인 전략 전체 로드 |
| `execute_entry(api, strategy)` | 스크리닝 결과 종목 매수 실행. 잔고 확인 후 매수 주문. **실패 시 최대 3회 재시도** (1초, 2초 간격) |
| `run(strategy_id)` | 메인 루프. 전략별 entry_time 기준 매수, 30초마다 포지션 모니터링, 15:20 강제매도, 15:30 결산 |

#### 루프 내 주요 처리

- **자정 초기화:** `t < 900`이고 새 날짜이면 `entry_done` 초기화 (다음날 재매수 허용)
- **전략별 매수 window:** `entry_t <= t < entry_t + 10` (10분 내 1회만 실행, `entry_done`으로 중복 방지)
- **포지션 모니터링:** `910 <= t < 1520` 구간에서 `check_and_exit(api, strategies)` 호출
- **15:30 결산:** `get_balance()` 실패 시 텔레그램 오류 알림, `summary_sent_day`는 미설정으로 다음 루프에서 재시도

---

### `engine/screener.py` — 종목 스크리닝

전략 설정에 따라 매수 후보 종목을 선별해 반환. `universe.type` 값으로 유니버스 종류 분기.

#### 함수

| 함수 | 설명 |
|------|------|
| `get_execution_strength(price_info)` | 체결강도 계산 (`매수체결수량 / 매도체결수량 × 100`). 모의투자에서는 항상 0.0 |
| `is_upper_limit(price_info)` | 상한가 여부 (`현재가 >= 상한가`) |
| `_build_volume_candidates(api, univ, entry)` | 거래량 상위 종목 조회 후 등락률 필터 (기존 전략 1·2·3) |
| `_build_research_candidates(api, univ, entry)` | 당일 리서치 보고서 종목 조회 후 등락률·가격 필터 (전략 4) |
| `screen(api, strategy)` | 스크리닝 메인 함수. universe.type에 따라 유니버스 빌더 분기 후 공통 필터 적용 |

#### `screen()` 필터링 순서

1. **유니버스 결정**
   - `type: "research"` → `_build_research_candidates()` — 당일 리서치 보고서 종목
   - 그 외 → `_build_volume_candidates()` — 거래량 상위 N개
2. 등락률 범위 필터 (`min_change_rate` ~ `max_change_rate`)
3. 종목별 현재가 조회 후:
   - PER/PBR 필터 (전략에 설정된 경우)
   - RSI/MACD 필터 (전략에 설정된 경우)
   - 체결강도 계산
4. 체결강도 내림차순 정렬
5. 상한가 종목 제외 후 상위 `max_stocks`개 선택 (제외된 종목은 다음 순위로 대체)

---

### `engine/research.py` — 리서치 보고서 스크래퍼

네이버 증권 리서치 페이지(`finance.naver.com/research/company_list.naver`)에서 당일 보고서 종목을 수집.

| 함수 | 설명 |
|------|------|
| `_parse_page(html, today_str)` | HTML에서 오늘 날짜(`YY.MM.DD`) 행만 파싱. 종목 링크에서 코드 직접 추출 |
| `get_today_research(max_pages)` | 최대 `max_pages`페이지 순회해 당일 보고서 전체 수집. 중복 코드 제거 후 반환 |

**반환 필드:** `code` (종목코드), `name` (종목명), `firm` (증권사), `title` (보고서 제목)

---

### `engine/monitor.py` — 포지션 모니터링

보유 포지션의 실시간 손익을 추적하고 익절/손절 조건 충족 시 매도.

#### 포지션 파일 관리

| 함수 | 설명 |
|------|------|
| `load_positions()` | `positions.json` 로드. 파일 없으면 `{}` 반환 |
| `save_positions(positions)` | `positions.json`에 저장 |
| `add_position(code, name, buy_price, qty, strategy_id)` | 매수 완료 후 포지션 등록 |
| `remove_position(code)` | 매도 완료 후 포지션 제거 |

#### 손익 기록

| 함수 | 설명 |
|------|------|
| `record_trade(name, code, qty, buy_price, sell_price, reason)` | 매도 시 `daily_pnl.json`에 손익 누적. 날짜 바뀌면 자동 초기화 |
| `load_daily_pnl()` | 당일 손익 데이터 로드. 날짜 불일치 시 빈 데이터 반환 |

#### 모니터링

| 함수 | 설명 |
|------|------|
| `check_and_exit(api, strategies)` | 모든 포지션 순회. 포지션의 `strategy_id`로 해당 전략의 exit 조건 매칭 후 익절/손절 판단. 조건 충족 시 시장가 매도 → 알림 → 기록 → 포지션 제거 |

---

### `engine/indicators.py` — 기술적 지표

외부 라이브러리 없이 순수 Python으로 구현.

| 함수 | 설명 |
|------|------|
| `_ema(data, period)` | 지수이동평균(EMA) 계산 (내부용) |
| `rsi(closes, period=14)` | RSI 계산. 데이터 부족 시 중립값 50.0 반환 |
| `macd(closes, fast=12, slow=26, signal=9)` | MACD 라인·시그널 라인·히스토그램·골든크로스 여부 반환. 골든크로스: 전봉 `MACD ≤ Signal` → 현봉 `MACD > Signal` |

---

### `engine/notifier.py` — 텔레그램 알림

텔레그램 Bot API로 실시간 알림 발송. 전송 실패 시 로그만 남기고 프로그램 계속 실행.

| 함수 | 발송 시점 | 내용 |
|------|-----------|------|
| `send(message)` | 직접 호출 (결산 요약 등) | HTML 파싱 모드 지원 |
| `notify_buy(name, code, qty, price)` | 매수 체결 | 종목명·수량·금액 |
| `notify_sell(name, code, qty, buy_price, current, reason)` | 익절/손절/강제매도 | 매입가·현재가·수익률·손익금액 |
| `notify_no_signal()` | 스크리닝 결과 없음 | 고정 메시지 |
| `notify_error(message)` | 오류 발생 | 오류 내용 |

---

## 전략 설정 구조 (`strategies/*.json`)

```jsonc
{
  "id": "vol_surge",
  "name": "전략 이름",
  "enabled": true,                      // false 로 비활성화
  "schedule": {
    "entry_time": "09:10",              // 매수 실행 시각
    "monitor_interval_sec": 30          // 포지션 모니터링 주기(초)
  },
  "universe": {
    "market": "0",                      // 0: 전체, 1: 코스피, 2: 코스닥
    "top_volume": 100,                  // 거래량 상위 N개
    "min_price": 1000,
    "max_price": 200000
  },
  "entry": {
    "min_change_rate": 5.0,             // 등락률 하한 (%)
    "max_change_rate": 10.0,            // 등락률 상한 (%)
    "min_per": 5.0,                     // PER 하한 (선택)
    "max_per": 15.0,                    // PER 상한 (선택)
    "min_pbr": 0.3,                     // PBR 하한 (선택)
    "max_pbr": 1.5,                     // PBR 상한 (선택)
    "indicators": {                     // 기술적 지표 (선택)
      "rsi":  {"period": 14, "oversold": 35},
      "macd": {"fast": 12, "slow": 26, "signal": 9}
    },
    "max_stocks": 3,                    // 최대 매수 종목 수
    "amount_per_stock": 1000000         // 종목당 투자금 (원)
  },
  "exit": {
    "take_profit": 2.0,                 // 익절 기준 (%)
    "stop_loss": -2.0                   // 손절 기준 (%)
  }
}
```

---

## 활성 전략 현황

| # | 전략 | 진입 시각 | 유니버스 | 등락률 | 추가 필터 | 최대 종목 | 투자금/종목 |
|---|------|-----------|---------|--------|-----------|-----------|-------------|
| 1 | vol_surge | 09:10 | 거래량 상위 | +5% ~ +10% | — | 3개 | 100만원 |
| 4 | research_morning | 09:15 | 당일 리서치 보고서 | +5% ~ +10% | — | 3개 | 30만원 |
| 2 | value_buffett | 10:00 | 거래량 상위 | -5% ~ +3% | PER 5~15, PBR 0.3~1.5 | 3개 | 100만원 |
| 3 | rsi_macd | 10:30 | 거래량 상위 | -10% ~ +10% | RSI < 35, MACD 골든크로스 | 3개 | 100만원 |

---

## 런타임 데이터 파일

### `positions.json`

```json
{
  "005930": {
    "name": "삼성전자",
    "buy_price": 68000,
    "qty": 14,
    "strategy_id": "vol_surge"
  }
}
```

### `daily_pnl.json`

```json
{
  "date": "2026-04-30",
  "total_pnl": 69800,
  "trades": [
    {
      "name": "채비", "code": "0011T0", "qty": 41,
      "buy_price": 24100, "sell_price": 25200,
      "pnl": 45100, "reason": "익절"
    }
  ]
}
```

### `.token_cache.json`

```json
{
  "token": "eyJ...",
  "expires": "2026-05-03T08:00:00"
}
```

---

## 환경 설정

### `.env` (필수)

```env
KIS_APP_KEY=앱키
KIS_APP_SECRET=앱시크릿
KIS_ACCOUNT_NO=계좌번호_앞8자리
KIS_ACCOUNT_CD=01
```

### `config.py` 직접 수정 항목

```python
IS_PAPER = True   # 실투자 전환 시 False 로 변경
```

---

## 모의투자 환경 특이사항

| 항목 | 내용 |
|------|------|
| 체결강도 | 항상 0.0 반환 (서버 제한) → 정렬 기준 무의미, 정상 동작 |
| chk-holiday API | 모의투자 미지원 → `holidays` 라이브러리로 대체, 근로자의 날 수동 처리 |
| 주문번호(odno) | None 반환되는 경우 있음, 정상 |
| 서버 500 에러 | 9:10 장 초반 간헐적 발생 → 매수/잔고 조회 모두 3회 재시도로 대응 |
| BASE_URL | `openapivts.koreainvestment.com:29443` |

---

## 작업 스케줄러 설정

- **작업명:** `KIS_AutoTrader`
- **실행 명령:** `cmd.exe /c "...\auto_trading\run_trader.bat"`
- **실행 조건:** 월~금 09:00, 로그인 상태 필요
- **주의:** `Stop On Battery Mode` ON → **충전기 반드시 연결**

---

## 패키지 의존성

```
requests>=2.31.0
python-dotenv>=1.0.0
websocket-client>=1.7.0
holidays>=0.46
beautifulsoup4>=4.12.0
```

설치: `pip install -r requirements.txt`
