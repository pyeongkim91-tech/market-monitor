# Market Monitor

미국·한국 시장 breadth, Equal/Cap 비율, 지수·VIX, 거시·금리 지표를 모아 신호등 리포트와
시장온도(MTI)를 만들고 메일/텔레그램으로 보내는 Cloud Run 서비스.

- `monitor.py` — 데이터 수집과 리포트 생성
- `app.py` — Flask 엔드포인트 (`/` 헬스체크, `/run` 리포트 실행)

## 데이터 소스

| 항목 | 소스 |
|---|---|
| 시세·지수·환율·원자재 | Yahoo Finance (`yfinance`) |
| S&P500 구성종목 | Wikipedia |
| KOSPI200 구성종목 | KRX 정보데이터시스템 (`pykrx`), 실패 시 Wikipedia |
| 금리·스프레드·Sahm | FRED CSV (`fredgraph.csv`) |

KOSPI200 구성종목을 190개 미만으로 받으면 KOSPI200 breadth는 `N/A`로 표시한다.
틀린 종목 리스트로 계산하는 것보다 N/A가 낫다고 보기 때문이다.

## 환경변수

| 변수 | 필수 | 설명 |
|---|---|---|
| `RUN_TOKEN` | ✅ | `/run` 호출 토큰. 설정하지 않으면 `/run`은 항상 401을 반환한다 |
| `KRX_ID`, `KRX_PW` | 권장 | KRX 정보데이터시스템 로그인 정보 (TODO: pykrx 로그인 방식 확인 전) |
| `MTI_HISTORY_PATH` | 권장 | MTI 이력 파일 경로. 기본값 `mti_log.json`은 인스턴스가 재시작되면 사라진다 |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `EMAIL_FROM`, `EMAIL_TO` | 선택 | 메일 발송 |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | 선택 | 텔레그램 발송 |

비밀값(`RUN_TOKEN`, `KRX_PW`, `SMTP_PASSWORD`, `TELEGRAM_BOT_TOKEN`)은 Secret Manager에 넣고
`--set-secrets`로 주입한다. 이 값들은 저장소에 커밋하지 않는다.

## 배포 (Cloud Run)

```bash
gcloud run deploy market-monitor --source . --region asia-northeast3 \
  --timeout 900 --memory 1Gi \
  --set-secrets RUN_TOKEN=run-token:latest,KRX_ID=krx-id:latest,KRX_PW=krx-pw:latest \
  --add-volume name=state,type=cloud-storage,bucket=<BUCKET> \
  --add-volume-mount volume=state,mount-path=/state \
  --set-env-vars MTI_HISTORY_PATH=/state/mti_log.json
```

- `--timeout 900`: 종목 약 700개를 받는 데 몇 분 걸리므로 기본값 300초로는 부족할 수 있다.
- `--add-volume` / `--add-volume-mount`: Cloud Storage 버킷을 마운트해서 MTI 이력이 인스턴스 재시작 후에도 남게 한다.

Cloud Scheduler에서는 토큰을 헤더에 넣어 호출한다.

```bash
gcloud scheduler jobs update http market-monitor --uri "https://<SERVICE_URL>/run" \
  --update-headers "Authorization=Bearer <RUN_TOKEN>" --attempt-deadline 900s
```

`/run?notify=0`으로 호출하면 메일/텔레그램을 보내지 않고 리포트만 반환한다.
동시에 두 번 호출되면 두 번째 요청은 409를 반환한다.

## 로컬 실행

```bash
pip install -r requirements.txt
python monitor.py            # 리포트 출력 + 알림 발송 (알림 환경변수가 있을 때만)
```
