# 자동매매 시스템 v3.0 (KIS API)

한국투자증권 KIS Developers API 기반 자동매매 시스템.

---

## 아키텍처

```
Windows 작업 스케줄러 (MUS_Watchdog) — 매일 08:48 기동
  └─ start_watchdog.bat
       └─ watchdog.py               ← 감시 프로세스 (08:48~15:35)
            ├─ telegram_bot.py      ← 텔레그램 명령 처리
            ├─ runner.py vol_surge_0905
            ├─ runner.py vol_surge_0910
            ├─ runner.py vol_surge_0915
            ├─ runner.py vol_surge_fulltime_v2
            └─ runner.py vol_surge_fulltime_v3
```

- 전략마다 **독립 프로세스**로 runner.py가 실행됨
- watchdog.py가 60초마다 생존 확인, 죽으면 자동 재시작 + 텔레그램 알림
- 전략 활성화/비활성화는 JSON의 `"enabled"` 필드로 제어 (장 중에도 반영)

---

## 디렉토리 구조

```
auto_trading/
│
├── config.py                  # API 키, 계좌번호, BASE_URL 설정
├── kis_api.py                 # KIS API 래퍼 (토큰·시세·주문·잔고·분봉)
├── watchdog.py                # 감시 프로세스 (전략/봇 자동 재시작)
├── start_watchdog.bat         # 작업 스케줄러 진입점
├── launcher.py                # (구) 멀티전략 런처 — 현재 watchdog으로 대체
├── telegram_bot.py            # 텔레그램 명령 처리 봇
├── pnl_report.py              # 전략별 손익 리포트 출력
├── check_balance.py           # 잔고 확인 유틸
├── requirements.txt
├── .env                       # API 키 환경변수 (비공개)
├── .env.example
│
├── engine/
│   ├── runner.py              # 단일 전략 실행 루프 (진입·모니터링·마감매도·결산)
│   ├── screener.py            # 종목 스크리닝 + KODEX200 지수 방향 필터
│   ├── monitor.py             # 포지션 관리, 익절/손절, 손익 기록, FileLock
│   ├── notifier.py            # 텔레그램 알림
│   ├── indicators.py          # RSI, MACD 계산
│   └── research.py            # 네이버 증권 리서치 보고서 스크래퍼
│
├── strategies/
│   ├── vol_surge_0905.json        ✅ 활성 — 09:05 일반진입
│   ├── vol_surge_0910.json        ✅ 활성 — 09:10 일반진입
│   ├── vol_surge_0915.json        ✅ 활성 — 09:15 일반진입
│   ├── vol_surge_fulltime_v2.json ✅ 활성 — 09:01~15:00 연속진입 (ETF 제외)
│   ├── vol_surge_fulltime_v3.json ✅ 활성 — 09:01~15:00 연속진입 (ETF 포함)
│   ├── vol_surge_fulltime.json    ⛔ 비활성
│   ├── vol_surge_vwap_0905.json   ⛔ 비활성
│   ├── vol_surge_vwap_0910.json   ⛔ 비활성
│   ├── vol_surge_vwap_0915.json   ⛔ 비활성
│   ├── research_morning.json      ⛔ 비활성
│   ├── value_buffett.json         ⛔ 비활성
│   └── rsi_macd.json              ⛔ 비활성
│
├── positions.json             # 런타임: 현재 보유 포지션 (FileLock 보호)
├── daily_pnl.json             # 런타임: 당일 손익 기록
├── strategy_history.json      # 전략별 누적 손익 이력
├── .token_cache.json          # 런타임: KIS 액세스 토큰 캐시
├── watchdog.log               # 워치독 실행 로그
└── trading_<sid>.log          # 전략별 실행 로그
```

---

## 활성 전략 현황

| 전략 ID | 진입 방식 | 진입 시간 | 등락률 | 체결강도 | 익절 | 손절 | ETF | ETN |
|---------|-----------|-----------|--------|----------|------|------|-----|-----|
| vol_surge_0905 | 일반 1회 | 09:05 | +5%~+10% | ≥120 (3분봉) | +2% | -1% | 제외 | 제외 |
| vol_surge_0910 | 일반 1회 | 09:10 | +5%~+10% | ≥120 (3분봉) | +2% | -1% | 제외 | 제외 |
| vol_surge_0915 | 일반 1회 | 09:15 | +5%~+10% | ≥120 (3분봉) | +2% | -1% | 제외 | 제외 |
| vol_surge_fulltime_v2 | 연속 반복 | 09:01~15:00 | +2%~+15% | ≥120 (즉시) | +1% | -1% | 제외 | 제외 |
| vol_surge_fulltime_v3 | 연속 반복 | 09:01~15:00 | +2%~+15% | ≥120 (즉시) | +1% | -1% | 포함 | 제외 |

- **종목당 투자금**: 당일 예수금의 10% (시작 시 동적 계산)
- **전략 간 중복 방지**: 같은 종목을 두 전략이 동시에 보유/예약 불가 (`reserve_or_skip`)

---

## 매수 실행 흐름 (execute_entry)

```
screen() — 종목 스크리닝
    ↓
종목별 반복:
    ├─ reserve_or_skip() — 타 전략 중복 예약 확인
    ├─ 잔고 확인
    ├─ 체결강도 재확인 (매수 직전, min_execution_strength 있을 때)
    ├─ KODEX200 3분봉 상승 확인 (매수 직전)  ← v3.0 신규
    └─ api.buy() — 시장가 매수 (실패 시 최대 3회 재시도)
```

### 일반 전략 vs 연속진입 전략의 KODEX200 필터 차이

| | 일반 전략 (0905/0910/0915) | 연속진입 전략 (v2/v3) |
|---|---|---|
| 루프 레벨 체크 | ✅ 진입 시간에 1회 체크, 하락이면 당일 전체 중단 | ❌ 없음 |
| 매수 직전 체크 | ✅ 종목별 체크 (하락이면 해당 종목 스킵) | ✅ 종목별 체크 (하락이면 해당 종목 스킵) |
| 하락 시 | 당일 매수 없음 | 이번 사이클 스킵, 다음 사이클에 재시도 |

---

## 스크리닝 필터 순서 (screener.py)

1. 거래량 상위 종목 조회
2. ETN 무조건 제외 (종목명에 "ETN" 포함 시)
3. ETF 조건부 제외 (`exclude_etf: true` 전략만)
4. 등락률 범위 필터 (`min_change_rate` ~ `max_change_rate`)
5. 상한가 종목 제외
6. 체결강도 기준 정렬 → 상위 1개 선택
7. VWAP 비율 계산 (기록용)

---

## 손익 계산 기준

| 항목 | 내용 |
|------|------|
| 매도세 (KOSPI) | 0.03% (증권거래세) + 0.15% (농어촌특별세) = 0.18% |
| 매도세 (KOSDAQ) | 0.18% |
| 수수료 | 약 0.015% (양방향 합산) |
| 순손익 | 매도금액 - 매수금액 - 세금 - 수수료 |

---

## 런타임 데이터 파일

### `positions.json`

```json
{
  "005930_vol_surge_0905": {
    "code": "005930",
    "name": "삼성전자",
    "buy_price": 68000,
    "qty": 14,
    "strategy_id": "vol_surge_0905",
    "execution_strength": 135.2,
    "change_rate": 6.5,
    "vwap_ratio": 1.012
  }
}
```

### `daily_pnl.json`

```json
{
  "date": "2026-05-13",
  "trades": [
    {
      "name": "채비", "code": "0011T0", "qty": 41,
      "buy_price": 24100, "sell_price": 25200,
      "gross_pnl": 45100, "cost": 4100, "pnl": 41000,
      "reason": "익절", "strategy_id": "vol_surge_0905"
    }
  ]
}
```

---

## 작업 스케줄러 설정

| 작업명 | 상태 | 실행 시각 | 역할 |
|--------|------|-----------|------|
| MUS_Watchdog | ✅ 활성 | 매일 08:48 | 워치독 + 전략 프로세스 관리 |
| MUS_AutoTrading | ⛔ 비활성 | — | (구) 단일 launcher |
| MUS_TelegramBot | ⛔ 비활성 | — | (구) 단일 봇 실행 |

- 실행 계정: S4U (로그인 없이 백그라운드 실행)
- 실행 시간 제한: 8시간
- 실패 시 재시작: 1분 간격, 최대 3회

---

## 패키지 의존성

```
requests>=2.31.0
python-dotenv>=1.0.0
holidays>=0.46
beautifulsoup4>=4.12.0
filelock>=3.0.0
```

설치: `pip install -r requirements.txt`

---

## 변경 이력

| 버전 | 주요 변경 |
|------|-----------|
| v1.0 | 단일 runner.py, 다전략 순차 실행 |
| v2.0 | 멀티프로세스 (launcher.py), 전략별 독립 runner, FileLock 도입 |
| v3.0 | watchdog.py 도입, ETN 별도 분리 제외, KODEX200 3분봉 지수 필터, 매수 직전 체결강도/지수 재확인, 장마감 강제매도 5회 재시도, 세금율 수정(0.18%) |
