# 작업 지시: market-monitor-phase1 품질 검토·개선 및 market-monitor 흡수

> 이 문서는 로컬 코딩 에이전트에게 그대로 전달하는 작업 지시서입니다.
> 작성 시점: 2026-10-01 KST. 클라우드 세션에서 읽기 전용으로 1차 검토한 결과를 바탕으로 합니다.

---

## 0. 역할과 원칙

당신은 개인 투자 모니터링 시스템의 시니어 파이썬 엔지니어입니다.
아래 순서(Phase A → B → C → D)대로 작업하고, **각 Phase가 끝날 때마다 멈추고 결과를 보고**하세요.
사용자 승인 없이 다음 Phase로 넘어가지 않습니다.

공통 원칙
- 모든 변경은 별도 브랜치에서 합니다. `main`에 직접 push하지 않습니다.
- 변경 전에 반드시 관련 코드를 읽고, 추측으로 고치지 않습니다. 확인하지 못한 것은 "미확인"이라고 적습니다.
- 한 커밋에는 한 가지 목적만 담습니다. 커밋 메시지는 무엇을·왜 바꿨는지 적습니다.
- 테스트를 지우거나 skip 처리해서 통과시키지 않습니다. 실패 원인을 고칩니다.
- 시크릿(`FRED_API_KEY`, `TELEGRAM_*`, `KRX_ID/PW`)을 코드, 로그, 커밋에 남기지 않습니다.
- 텔레그램 실발송은 사용자가 명시적으로 허락하기 전까지 하지 않습니다. 항상 dry-run으로 확인합니다.
- 투자 판단을 제공하지 않는다는 리포트의 면책 문구(Notice)는 유지합니다.

---

## 1. 대상 레포

| 레포 | 위치 | 역할 | 이번 작업에서의 지위 |
|---|---|---|---|
| `pyeongkim91-tech/market-monitor-phase1` (private) | **기준 레포** | 사모신용·BDC·시장 모니터. `src/` 모듈 구조, 테스트 28개 파일, 약 10,400줄 | 품질 개선 + 흡수 대상 |
| `pyeongkim91-tech/market-monitor` (public) | 흡수될 레포 | `monitor.py` 한 파일(517줄) + smoke test. breadth, Equal/Cap, VIX, 금리, MTI | phase1에 없는 기능만 이식 |
| `pyeongkim91-tech/dance-near-the-door` (private) | **독립 유지** | AI 인프라 사이클 반전 증거 모니터. 엄격한 명세·리뷰·앵커링 거버넌스 | **이번 작업에서 수정 금지** |

DNTD는 읽기만 하고, 어떤 파일도 바꾸지 않습니다. 공통 코드를 뽑아내는 작업도 이번 범위가 아닙니다.

---

## 2. Phase A — 현황 파악 (코드 변경 없음)

목표: 수정 전에 사실을 확정합니다.

1. phase1을 clone하고 가상환경을 만들어 `pip install -e ".[dev]"` 후 `pytest -q`를 실행합니다.
2. 아래 "1차 검토 결과"와 직접 대조해서, 재현되는 것과 재현되지 않는 것을 표로 정리합니다.
3. `README.md`, `docs/current_status_and_milestones.md`, `docs/ops_log.md`, `docs/nav_update_runbook.md`, `codex_phase1_review.md`를 읽고, 문서와 실제 코드가 어긋난 곳을 찾습니다(예: 문서의 테스트 개수, 발송 시각).
4. `.github/workflows/daily.yml`과 `launch/*.plist`를 읽고, **현재 실제로 메시지를 보내는 경로가 무엇인지** 확정합니다. 근거: 오늘(2026-10-01) 메시지는 10:52 KST에 도착했는데 workflow cron은 08:00/08:30 KST입니다. GitHub Actions 지연인지, launchd(로컬)가 따로 도는지, 둘이 중복인지 확인합니다.

산출물: Phase A 보고서(마크다운, 대화창에 출력). 불확실한 항목은 사용자에게 질문 목록으로 정리합니다.

### 1차 검토 결과 (클라우드 세션에서 확인된 것)

| # | 심각도 | 내용 | 상태 |
|---|---|---|---|
| 1 | 높음 | `tests/test_bdc_fundamentals.py`에서 27개 테스트가 2026-10-01 기준으로 실패. 날짜를 2026-05-15로 고정(freezegun)하면 전부 통과. 즉 로직 버그가 아니라 **실제 시계 의존**. `src/indicators/bdc_fundamentals.py`에는 `today` 주입 파라미터가 있으나(`today: date \| None`, `_date.today()` 폴백) 테스트가 이를 쓰지 않음. 공시 종료일이 120일을 넘기면 신선도 필터에 걸림 | 재현됨 |
| 2 | 높음 | 종합 판정 구조: 종목 상태 = 지표 중 최악값, 전체 상태 = 종목 중 최악값(`_worst`, 같은 파일 20~21행 설명). 한 종목만 🔴이어도 전체가 🔴. 오늘 21종목 중 12개가 🔴이며 OFS 같은 소형주가 전체 신호를 결정 | 코드로 확인 |
| 3 | 중간 | D/E 임계값이 빡빡해 보임. 1.3배 안팎부터 🔴(BXSL 1.32, FSK 1.31, NMFC 1.34가 🔴, TSLX 1.29는 🟠). BDC 법정 한도는 자산커버리지 150%(D/E 약 2.0배), 업계 통상은 1.0~1.25배 | **임계값 코드 미확인** — `src/thresholds.py`에서 확인 필요 |
| 4 | 중간 | NAV가 수동 CSV(`data/nav_manual.csv`)라서 stale. OBDC Q1 2026 NAV(공시 148일 경과), ARCC 156일 경과. 같은 메시지의 BDC Fundamentals는 2026Q2 공시를 쓰므로 시점이 서로 어긋남. 이 때문에 OBDC NAV 할인율 27.2%의 신뢰도가 낮음 | 데이터 확인 |
| 5 | 중간 | 결측 커버리지: PIK 13/21, non-accrual 18/21인데 평균과 종합 판정이 그대로 나감. 일부 종목(MAIN, TRIN, SLRC 등)은 지표가 대부분 비어 있음 | 메시지 기준, 코드 미확인 |
| 6 | 낮음 | Reuters 2026-05-30 BDC 샘플(51개, -2.35%)이 4개월 지난 일회성 수치인데 상시 지표처럼 노출 | 메시지 기준 |
| 7 | 낮음 | 데이터 기준일 혼재(FRED 일간 09-29, NFCI 09-25, 주가 09-30). 파생 지표(CCC-BB 갭 등)가 서로 다른 날짜로 계산되는지 미확인 | 미확인 |

### 철회된 의심 (다시 문제 삼지 말 것)
- "PFLT·ARCC의 종목 아이콘과 세부 신호 불일치": 버그가 아님. 종목 아이콘이 지표 최악값이므로 일관됨.

### 클라우드 세션에서 검증하지 못한 것 (로컬에서 반드시 확인)
- 클라우드 환경의 egress 프록시가 `fred.stlouisfed.org`를 차단해서 **FRED 실데이터 검증을 못 했음**.
- 확인할 것: 메시지의 UST10Y 5.26%, HY OAS 3.08%, CCC OAS 11.57%, IG OAS 0.84%, BBB OAS 1.02%가 FRED 원본과 일치하는지. 특히 UST10Y 수준 대비 HY OAS가 낮다는 점과 CCC OAS와의 간극이 시리즈 ID/단위 문제인지 시장 현실인지.
- 코드상 시리즈 ID는 `src/constants.py`에 있음(HY `BAMLH0A0HYM2`, CCC `BAMLH0A3HYC`, BB `BAMLH0A1HYBB`, IG `BAMLC0A0CM`, BBB `BAMLC0A4CBBB`, `DGS10`, `SOFR`, `NFCI`). 단위 변환은 `src/sources/fred.py`에서 `change × 100`으로 bp 계산.

---

## 3. Phase B — 신뢰성 복구 (테스트·데이터 정합성)

브랜치: `fix/phase1-quality-b`

### B1. 테스트의 시계 의존 제거 (최우선)
- `tests/test_bdc_fundamentals.py`의 실패 27개를 고칩니다. 방법 우선순위:
  1. 이미 있는 `today` 주입 파라미터를 테스트에서 명시적으로 넘깁니다.
  2. 주입 경로가 없는 함수는 `today`를 받도록 최소한으로 확장합니다(기본값은 현재 동작 유지).
  3. 불가피할 때만 freezegun을 dev 의존성에 추가합니다.
- 다른 테스트 파일에도 같은 패턴(`date.today()`, `datetime.now()`)이 있는지 `grep`으로 찾아 같은 방식으로 정리합니다.
- 완료 기준: 날짜와 무관하게 `pytest -q` 전체 통과. 검증 방법으로 시스템 날짜를 바꾸거나 freezegun으로 2026-05-15, 2026-10-01, 2027-01-01 세 시점에서 모두 통과해야 합니다.
- 회귀 방지: 테스트에서 실제 날짜 함수를 부르면 실패하게 하는 가드(conftest의 autouse fixture 등)를 검토합니다.

### B2. 임계값 점검
- `src/thresholds.py`와 `src/indicators/bdc_fundamentals.py`의 BDC 임계값(non-accrual, PIK, NAV QoQ, 배당커버리지, D/E)을 표로 정리합니다.
- 각 임계값에 근거(SEC 기준, 과거 분포, 업계 통상)가 있는지 확인하고, 근거가 없는 것은 목록으로 보고합니다.
- D/E는 법정 한도와 업계 통상 수준 대비 적절한지 의견을 냅니다. **임계값 변경은 이 단계에서 하지 말고 제안만** 합니다.

### B3. 데이터 정합성 검증 스크립트
- 로컬에서 `FRED_API_KEY`로 실제 FRED를 호출해 위 5개 스프레드와 금리를 원본과 대조하는 일회성 스크립트를 `scripts/verify_fred_values.py`로 만듭니다.
- 출력: 시리즈 ID, 앱이 계산한 값, FRED 원본 최신값, 날짜, 일치 여부. 불일치가 있으면 원인(단위, 시리즈 ID, 지연일)을 적습니다.
- 파생 지표(CCC-BB 갭 등)가 서로 다른 기준일 데이터로 계산되는지 확인합니다.

### B4. 발송 경로 정리
- Phase A에서 확정한 실제 발송 경로와 cron 시각의 불일치를 보고합니다.
- 중복 발송 가능성(Actions와 launchd 동시)이 있으면 사용자에게 어느 쪽을 정본으로 할지 묻습니다.

Phase B 종료 시 보고: 변경 파일 목록, 테스트 결과(세 시점), 발견된 데이터 불일치, 임계값 제안표.

---

## 4. Phase C — 판정 로직 개선 (설계 판단이 필요하므로 선택지부터 제시)

브랜치: `feat/phase1-aggregation-c`

코드를 바꾸기 전에 아래 선택지를 비교해서 **사용자에게 선택을 요청**하세요.

### C1. 종합 판정 방식
현재: `전체 = max(종목별 최악)`. 문제: 변별력이 없고 소형주 하나가 전체를 좌우함.
후보:
- (a) 🔴 종목 비율 기준(예: 20% 이상이면 🔴, 10% 이상이면 🟠)
- (b) NAV 또는 시가총액 가중 평균 점수
- (c) 현행 유지 + "🔴 N/21개 (가중 비중 X%)"를 함께 표시
각 후보에 대해 오늘 데이터(21종목)를 넣었을 때 결과가 어떻게 달라지는지 시뮬레이션해서 보여주세요.

### C2. 결측 처리
- 지표별 커버리지가 낮을 때(예: PIK 13/21) 평균을 어떻게 표시할지, 종합 판정에 어떻게 반영할지 제안합니다.
- 이미 있는 per-metric coverage floor 로직(`TestPerMetricCoverage` 등)과 일관되게 설계합니다.

### C3. NAV 자동 갱신 검토
- `data/nav_manual.csv` 수동 관리를 SEC EDGAR 기반 자동 갱신으로 대체할 수 있는지 `src/sources/bdc_edgar.py`, `sec_edgar.py` 기준으로 검토합니다.
- 자동화가 어렵다면 최소한 NAV 시점이 Fundamentals 분기와 어긋날 때 NAV 할인율을 🟢/🟠 같은 신호로 쓰지 않고 "참고용(stale)"로 강등하는 안을 제안합니다.

### C4. Reuters 샘플 등 정적 수치
- 날짜가 있는 일회성 수치는 일정 기간(예: 60일) 지나면 자동으로 숨기거나 "(stale)"로 표시하는 방안을 제안합니다.

각 변경에는 단위 테스트를 추가하고, 기존 리포트 형식(텔레그램 메시지 구조)을 깨지 않는지 `tests/test_render.py`로 확인합니다.

---

## 5. Phase D — market-monitor 흡수

브랜치: `feat/absorb-market-monitor-d`

1. `market-monitor`의 `monitor.py` 기능을 phase1과 항목별로 비교하는 표를 만듭니다.
   - 열: 기능 / market-monitor 구현 위치 / phase1 대응 모듈 / 중복 여부 / 이식 필요 여부
   - 후보 기능: S&P500·KOSPI200 breadth(>50DMA, >200DMA), Equal/Cap 비율, 지수·VIX, Sahm rule, 시장온도(MTI)와 3일 이력, KOSPI200 구성종목 190개 미만 시 N/A 처리, pykrx 실패 시 Wikipedia 대체.
2. phase1에 이미 있는 것(`src/indicators/breadth.py`, `market_temperature.py`, `src/sources/breadth.py` 등)은 **이식하지 않고** 차이만 기록합니다.
3. phase1에 없는 기능만 phase1의 모듈 구조(`src/sources`, `src/indicators`)에 맞춰 이식합니다. 한 파일을 통째로 복사하지 않습니다.
4. 이식한 기능마다 테스트를 추가합니다(market-monitor의 `tests/smoke_test.py`가 가짜 시세로 리포트 전체 경로를 점검하는 방식을 참고).
5. **메시지가 하나로 합쳐지는지** 확인합니다. 지금은 07:50(market-monitor), 08:00/08:30(phase1) 두 개의 메시지가 따로 옵니다. 통합 후 발송 1회, 시각 1개로 정리하는 안을 제시합니다.
6. `market-monitor` 레포는 이 단계에서 **삭제하지 않습니다.** README에 "phase1로 이전됨" 안내를 넣는 안만 제안하고, archive 여부는 사용자가 결정합니다.
7. 주의: `market-monitor`는 public이고 phase1은 private입니다. 통합 후 공개 범위는 private 기준으로 하되, 이 결정은 사용자에게 확인합니다. 코드나 문서에 개인 이메일 등이 들어 있다면(예: SEC User-Agent) public으로 노출되지 않게 합니다.
8. Secrets·캐시 이전 체크리스트를 문서로 만듭니다(`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `KRX_ID`, `KRX_PW`, `FRED_API_KEY`, MTI 이력 캐시). 값 자체는 어디에도 쓰지 않습니다.

---

## 6. 보고 형식

각 Phase 종료 시 다음을 포함해서 보고하세요.
1. 한 줄 요약
2. 변경한 파일과 이유(표)
3. 실행한 명령과 실제 결과(통과/실패 개수 포함). 실행하지 못한 것은 "미실행"이라고 명시
4. 재현되지 않았거나 1차 검토와 다르게 나온 항목
5. 사용자에게 필요한 결정(번호 매긴 질문)
6. 다음 Phase에서 할 일

## 7. 하지 말 것
- DNTD 레포 수정, 공통 라이브러리 분리
- 임계값·판정 로직을 사용자 승인 없이 바꾸기(Phase B는 제안만, Phase C는 선택 후 구현)
- `market-monitor` 레포 삭제·archive
- 텔레그램 실발송, 시크릿 출력
- 테스트 삭제·skip, 의존성 대규모 업그레이드
