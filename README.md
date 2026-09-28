# Market Monitor

미국·한국 시장 breadth, Equal/Cap 비율, 지수·VIX, 거시·금리 지표를 모아 신호등 리포트와
시장온도(MTI)를 만들고 텔레그램으로 보낸다. GitHub Actions에서 평일 07:50 KST에 실행된다.

- `monitor.py` — 데이터 수집, 리포트 생성, 텔레그램 발송
- `.github/workflows/market-monitor.yml` — 예약 실행
- `tests/smoke_test.py` — 가짜 시세로 리포트 전체 경로 점검 (매 실행 전 수행)

## 데이터 소스

| 항목 | 소스 |
|---|---|
| 시세·지수·환율·원자재 | Yahoo Finance (`yfinance`) |
| S&P500 구성종목 | Wikipedia |
| KOSPI200 구성종목 | KRX 정보데이터시스템 (`pykrx`), 실패 시 Wikipedia |
| 금리·스프레드·Sahm | FRED 공식 API (`FRED_API_KEY`), 키가 없으면 `fredgraph.csv` |

KOSPI200 구성종목을 190개 미만으로 받으면 KOSPI200 breadth는 `N/A`로 표시한다.
틀린 종목 리스트로 계산하는 것보다 N/A가 낫다고 보기 때문이다.

## 설정 (GitHub 저장소 → Settings → Secrets and variables → Actions)

| Secret | 설명 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | 텔레그램 봇 토큰 (@BotFather) |
| `TELEGRAM_CHAT_ID` | 받을 채팅 ID |
| `KRX_ID`, `KRX_PW` | KRX 정보데이터시스템 로그인 정보 (pykrx가 읽음) |
| `FRED_API_KEY` | FRED API 키 (무료, fredaccount.stlouisfed.org). 없으면 거시·금리 지표가 N/A가 되기 쉬움 |

## 실행

- **예약 실행:** `main` 브랜치 기준 평일 07:50 KST (GitHub 사정으로 몇 분~수십 분 늦을 수 있음)
- **수동 실행:** Actions 탭 → Market Monitor → Run workflow. `notify`를 끄면 발송 없이 리포트만 확인
- **`claude/**` 브랜치 push:** 발송 없이 검증용으로 실행

리포트 본문은 각 실행의 Summary 화면에도 남는다.
MTI 이력(최근 3일 평균)은 `main`에서 실행될 때만 Actions 캐시에 저장된다.

## 로컬 실행

```bash
pip install -r requirements.txt
python tests/smoke_test.py         # 네트워크 없이 점검
NOTIFY=false python monitor.py     # 실제 데이터로 리포트만 출력
```
