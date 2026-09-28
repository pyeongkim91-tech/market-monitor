# monitor.py — Market Monitor 리포트 생성 (app.py / Cloud Run에서 import)
import os, json, logging
from io import StringIO
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
import yfinance as yf
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------- 기본설정 ----------------
# 200일 SMA + 4주 변화에 필요한 만큼만 받음 (영업일 200일 ≈ 달력 290일, 여유 포함)
LOOKBACK_DAYS = 450
START   = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
SMA_WIN = 200
W4      = 20  # 4주(영업일) 근사
KST     = timezone(timedelta(hours=9))

# 지수/ETF 티커
US_EQW, US_CAP = "RSP", "VOO"
KOSPI, KOSDAQ, VIX_TK = "^KS11", "^KQ11", "^VIX"

# 한국 EW/Cap (정정 반영)
KR_EQW, KR_CAP = "252000.KS", "069500.KS"  # 252000 = TIGER 200 Equal Weighted / 069500 = KODEX200
KR_EQW_NAME, KR_CAP_NAME = "TIGER 200 Equal Weighted", "KODEX200"

logging.basicConfig(level=logging.INFO)

# ---------------- HTTP 세션(재시도) ----------------
_session = None
def _sess():
    global _session
    if _session is None:
        s = requests.Session()
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[429,500,502,503,504])
        s.mount("https://", HTTPAdapter(max_retries=retry))
        s.mount("http://", HTTPAdapter(max_retries=retry))
        s.headers.update({
            "User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0",
            "Accept-Language":"en-US,en;q=0.9,ko;q=0.8",
        })
        _session = s
    return _session

def _http_get(url:str)->str:
    r = _sess().get(url, timeout=15)
    r.raise_for_status()
    return r.text

# ---------------- 유틸 ----------------
def _pct(x):
    try: return f"{x*100:.1f}%"
    except: return "N/A"

def last(s: pd.Series):
    s = s.dropna()
    return s.iloc[-1] if not s.empty else np.nan

def pct_change_weeks(s: pd.Series, weeks=W4):
    s = s.dropna()
    if len(s) <= weeks: return np.nan
    return s.iloc[-1] / s.iloc[-weeks-1] - 1.0

def normalize_yield_pct(s: pd.Series):
    s = s.dropna()
    if s.empty: return s
    # ^TNX, ^IRX 등 10배 스케일 방어
    return s/10.0 if s.iloc[-1] > 20 else s

# yfinance 결과에서 종가 시리즈 안전 추출
def pick_close(df, tk:str):
    try:
        if df is None or len(df)==0: return pd.Series(dtype="float64")
        if isinstance(df.columns, pd.MultiIndex):
            # 멀티인덱스 형태 (ticker, field)
            if tk in df.columns.get_level_values(0):
                sub = df[tk]
                if "Adj Close" in sub and not sub["Adj Close"].dropna().empty:
                    return sub["Adj Close"].rename(tk).dropna()
                if "Close" in sub and not sub["Close"].dropna().empty:
                    return sub["Close"].rename(tk).dropna()
                # Fallback: 첫 컬럼
                c = sub.columns[0]
                return sub[c].rename(tk).dropna()
            return pd.Series(dtype="float64")
        else:
            # 단일 프레임
            if "Adj Close" in df:
                s = df["Adj Close"]
                if isinstance(s, pd.DataFrame):
                    # 다티커일 수 있음
                    return (s[tk] if tk in s.columns else s.iloc[:,0]).dropna()
                return s.dropna()
            if "Close" in df:
                s = df["Close"]
                if isinstance(s, pd.DataFrame):
                    return (s[tk] if tk in s.columns else s.iloc[:,0]).dropna()
                return s.dropna()
            # yfinance가 Series로 반환하는 엣지 처리
            if isinstance(df, pd.Series):
                return df.dropna()
            # 마지막 Fallback: 첫 컬럼
            return df.iloc[:,0].dropna()
    except Exception as e:
        logging.exception(f"pick_close failed for {tk}: {e}")
        return pd.Series(dtype="float64")

def yf_series(ticker:str, start=START) -> pd.Series:
    try:
        df = yf.download(ticker, start=start, progress=False, group_by="ticker",
                         threads=False, auto_adjust=False)
        return pick_close(df, ticker)
    except Exception as e:
        logging.exception(f"yf_series({ticker}) failed: {e}")
        return pd.Series(dtype="float64")

# ---------------- 위키 티커 ----------------
def _norm_kr_code(s: str) -> str:
    if not isinstance(s,str): return s
    s=s.strip()
    if not s: return s
    if s.upper().endswith((".KS",".KQ")): return s
    if s.isdigit() and len(s)==6: return s + ".KS"
    return s

def get_sp500_tickers():
    urls = [
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        "https://en.wikipedia.org/wiki/List_of_S&P_500_companies",
    ]
    for url in urls:
        try:
            soup = BeautifulSoup(_http_get(url), "html.parser")
            table = soup.find("table", {"id":"constituents"}) or soup.find("table")
            if not table: continue
            df = pd.read_html(StringIO(str(table)))[0]
            col = "Symbol" if "Symbol" in df.columns else ("Ticker symbol" if "Ticker symbol" in df.columns else None)
            if not col: continue
            syms = df[col].astype(str).str.strip().tolist()
            return [s.replace(".","-") for s in syms if s]
        except Exception as e:
            logging.exception(f"S&P500 ticker fetch failed ({url}): {e}")
    return []

KOSPI200_MIN = 190  # 구성종목 수 품질 기준 (정원 200)

def _kospi200_from_krx():
    """KRX 정보데이터시스템(pykrx)에서 KOSPI200(지수코드 1028) 구성종목.
    로그인 정보는 환경변수 KRX_ID / KRX_PW 로 전달 (코드/저장소에 두지 않음)."""
    try:
        from pykrx import stock
    except ImportError:
        logging.warning("pykrx not installed; skipping KRX source")
        return []
    today = datetime.now(KST)
    # 휴장일/장 시작 전 대비: 최근 7일을 거슬러 올라가며 시도
    for d in range(7):
        day = (today - timedelta(days=d)).strftime("%Y%m%d")
        try:
            codes = stock.get_index_portfolio_deposit_file("1028", day)
            codes = [c for c in codes if isinstance(c, str) and len(c) == 6 and c.isdigit()]
            if len(codes) >= KOSPI200_MIN:
                return [c + ".KS" for c in codes]
        except Exception as e:
            logging.warning(f"KRX KOSPI200 fetch failed for {day}: {e}")
    return []

def _kospi200_from_wiki():
    """Wikipedia 표의 코드 컬럼에서만 추출 (페이지 전체 숫자 긁기 금지)"""
    try:
        tables = pd.read_html(StringIO(_http_get("https://en.wikipedia.org/wiki/KOSPI_200")))
    except Exception as e:
        logging.exception(f"KOSPI200 wiki fetch failed: {e}")
        return []
    codes = set()
    for df in tables:
        df.columns = [str(c).strip() for c in df.columns]
        for key in ["Code", "Ticker", "Symbol", "KRX code", "Stock code"]:
            if key in df.columns:
                s = df[key].astype(str).str.strip().str.zfill(6)
                codes.update(s[s.str.fullmatch(r"\d{6}", na=False)])
    return [c + ".KS" for c in sorted(codes)]

def get_kospi200_tickers():
    """KRX 우선, 실패 시 Wikipedia. 품질 기준 미달이면 빈 리스트 (틀린 값보다 N/A가 낫다)"""
    tickers = _kospi200_from_krx()
    if tickers:
        return tickers
    tickers = _kospi200_from_wiki()
    if len(tickers) >= KOSPI200_MIN:
        return tickers
    logging.warning(f"KOSPI200 tickers insufficient ({len(tickers)}); breadth will be N/A")
    return []

# ---------------- 브레드스 ----------------
def _to_close_matrix(px, chunk):
    if px is None or px.empty: return None
    try:
        if isinstance(px.columns, pd.MultiIndex):
            cols=[]
            for t in chunk:
                if t in px.columns.get_level_values(0):
                    sub = px[t]
                    s=None
                    if "Adj Close" in sub and not sub["Adj Close"].dropna().empty: s=sub["Adj Close"]
                    elif "Close" in sub and not sub["Close"].dropna().empty:      s=sub["Close"]
                    if s is not None: cols.append(s.rename(t))
            return pd.concat(cols, axis=1) if cols else None
        else:
            # 단일 프레임
            base = None
            if "Adj Close" in px: base = px["Adj Close"].dropna(how="all")
            elif "Close" in px:   base = px["Close"].dropna(how="all")
            return base
    except Exception as e:
        logging.exception(f"_to_close_matrix failed: {e}")
        return None

def breadth_last_series_batched(tickers, start=START, sma_win=SMA_WIN, batch=80):
    sum_above, sum_valid = None, None
    for i in range(0, len(tickers), batch):
        chunk = [t for t in tickers[i:i+batch] if isinstance(t,str) and t.strip()]
        if not chunk: continue
        try:
            px = yf.download(chunk, start=start, progress=False, group_by="ticker",
                             threads=False, auto_adjust=False)
        except Exception as e:
            logging.exception(f"yfinance download failed chunk[{i}:{i+batch}]: {e}")
            continue
        close = _to_close_matrix(px, chunk)
        if close is None or close.empty: continue

        sma = close.rolling(sma_win, min_periods=max(10, sma_win//2)).mean()
        last_close, last_sma = close.iloc[-1], sma.iloc[-1]
        above = (last_close > last_sma).astype("int32")
        valid = (~pd.isna(last_close)).astype("int32")
        sum_above = above if sum_above is None else sum_above.add(above, fill_value=0)
        sum_valid = valid if sum_valid is None else sum_valid.add(valid, fill_value=0)

    if sum_valid is None or sum_valid.sum()==0:
        return pd.Series(dtype="float64")
    return (sum_above / sum_valid).astype("float64")

# ---------------- Ratio ----------------
def ratio_eqw_cap(eqw, cap, start=START):
    try:
        df = yf.download([eqw, cap], start=start, progress=False, group_by="ticker",
                         threads=False, auto_adjust=False)
        s_eqw, s_cap = pick_close(df, eqw), pick_close(df, cap)
        if s_eqw.empty or s_cap.empty: return pd.Series(dtype="float64")
        return (s_eqw / s_cap).dropna()
    except Exception as e:
        logging.exception(f"ratio_eqw_cap failed: {e}")
        return pd.Series(dtype="float64")

# ---------------- 인덱스 묶음 ----------------
def get_indices(start=START):
    out={}
    try:
        px = yf.download([KOSPI,KOSDAQ,VIX_TK,US_EQW,US_CAP],
                         start=start, progress=False, group_by="ticker",
                         threads=False, auto_adjust=False)
    except Exception as e:
        logging.exception(f"index download failed: {e}")
        px=None
    for tk in [KOSPI,KOSDAQ,VIX_TK,US_EQW,US_CAP]:
        out[tk] = pick_close(px, tk) if px is not None else pd.Series(dtype="float64")
    return out

# ---------------- 매크로(FRED) ----------------
# pandas_datareader는 관리 중단 → FRED CSV 엔드포인트 직접 사용
def fred(series_id, start=START):
    try:
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start}"
        df = pd.read_csv(StringIO(_http_get(url)))
        s = pd.Series(pd.to_numeric(df.iloc[:, 1], errors="coerce").values,
                      index=pd.to_datetime(df.iloc[:, 0]), name=series_id)
        return s.dropna()
    except Exception as e:
        logging.exception(f"FRED fetch failed for {series_id}: {e}")
        return pd.Series(dtype="float64")

# ---------------- 신호등 ----------------
def light_ratio(x):
    if np.isnan(x): return "⚪"
    return "🟢" if x>=0.60 else ("🟡" if x>=0.40 else "🔴")

def light_change(x, good_when_positive=True, tight=False):
    if np.isnan(x): return "⚪"
    th = (0.01, 0.03) if tight else (0.02, 0.05)  # 완만/보수적 선택
    good = x>=th[1] if good_when_positive else x<=-th[1]
    mid  = (th[0] <= x < th[1]) if good_when_positive else (-th[1] < x <= -th[0])
    bad  = x<=-th[1] if good_when_positive else x>=th[1]
    if good: return "🟢"
    if bad:  return "🔴"
    if mid:  return "🟡"
    return "🟡"

def compute_market_temperature(signals: dict):
    """🧮 신호등 평균으로 시장온도(MTI) 계산"""
    score_map = {'🟢': 1, '🟡': 0, '🔴': -1}
    valid_signals = [score_map.get(v, 0) for v in signals.values() if v in score_map]
    if not valid_signals:
        return 0.0, "⚪ 데이터 부족", "신뢰도: 낮음"
    avg = sum(valid_signals) / len(valid_signals)
    
    # 상태 구간 매핑
    if avg >= 0.6:
        state, posture = "🔥 과열권", "차익실현/리스크 관리"
    elif avg >= 0.2:
        state, posture = "🌤 완만한 강세", "완만한 확대"
    elif avg <= -0.2:
        state, posture = "❄️ 냉각기", "방어적 (리스크 축소)"
    else:
        state, posture = "⚪ 중립", "중립 (관망)"

    # 신뢰도 표시 (지표 수에 따라)
    reliability = "신뢰도: 높음" if len(valid_signals) >= 8 else \
                  "신뢰도: 보통" if len(valid_signals) >= 5 else "신뢰도: 낮음"

    return round(avg, 2), f"{state}", f"{posture} / {reliability}"

# MTI 이력: {날짜(KST): MTI}. 같은 날 여러 번 돌려도 하루 1개로 집계.
# Cloud Run 로컬 디스크는 휘발성 → MTI_HISTORY_PATH를 GCS 볼륨 마운트 경로로 지정할 것.
def update_mti_history(new_value, day: str, max_days=3):
    filename = os.environ.get("MTI_HISTORY_PATH", "mti_log.json")
    try:
        hist = {}
        if os.path.exists(filename):
            with open(filename, "r") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):  # 구버전(list) 이력은 날짜가 없어 버림
                hist = loaded
        hist[day] = new_value
        hist = dict(sorted(hist.items())[-max_days:])
        tmp = filename + ".tmp"
        with open(tmp, "w") as f:
            json.dump(hist, f)
        os.replace(tmp, filename)
        return sum(hist.values()) / len(hist), len(hist)
    except Exception as e:
        logging.exception(f"MTI history update failed: {e}")
        return np.nan, 0

# ---------------- 리포트 ----------------
def build_report(now_utc: datetime):
    ts = now_utc.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")

    # 1) 브레드스
    spx = get_sp500_tickers()
    k2  = get_kospi200_tickers()
    us_b = breadth_last_series_batched(spx, start=START, sma_win=SMA_WIN, batch=80) if spx else pd.Series(dtype="float64")
    kr_b = breadth_last_series_batched(k2,  start=START, sma_win=SMA_WIN, batch=80) if k2  else pd.Series(dtype="float64")
    us_ratio = us_b.mean() if us_b.notna().any() else np.nan
    kr_ratio = kr_b.mean() if kr_b.notna().any() else np.nan

    # 2) Equal vs Cap (4주)
    rsp_voo = ratio_eqw_cap(US_EQW, US_CAP, START)
    rsp_voo_4w = pct_change_weeks(rsp_voo)

    kr_eqw_cap = ratio_eqw_cap(KR_EQW, KR_CAP, START)
    kr_eqw_cap_4w = pct_change_weeks(kr_eqw_cap)

    # 3) 지수/변동성 (4주)
    idx = get_indices(START)
    ks11, kq11, vix = idx.get(KOSPI), idx.get(KOSDAQ), idx.get(VIX_TK)
    ks_4w  = pct_change_weeks(ks11) if not ks11.empty else np.nan
    kq_4w  = pct_change_weeks(kq11) if not kq11.empty else np.nan
    vix_4w = pct_change_weeks(vix)  if not vix.empty  else np.nan

    # 4) 금리·달러·원자재
    # 10Y: ^TNX -> 보정
    t10 = normalize_yield_pct(yf_series("^TNX"))
    # 2Y: FRED DGS2 사용(야후 ^UST2Y 불안정)
    t02 = fred("DGS2")
    # 달러지수: DXY(선물) 우선, 실패 시 FRED Broad Dollar 지수
    dxy = yf_series("DX-Y.NYB")
    if dxy.empty: dxy = yf_series("DX=F")
    dxy_label = "DXY" if not dxy.empty else "달러지수(광의, FRED)"
    if dxy.empty: dxy = fred("DTWEXBGS")  # Broad Dollar (Goods)
    # 환율/원자재
    usdk = yf_series("KRW=X")
    wti  = yf_series("CL=F")
    gold = yf_series("GC=F")
    if gold.empty: gold = yf_series("GLD")  # ETF 대체

    t10_c  = pct_change_weeks(t10)
    t02_c  = pct_change_weeks(t02)
    dxy_c  = pct_change_weeks(dxy)
    usdk_c = pct_change_weeks(usdk)
    wti_c  = pct_change_weeks(wti)
    gold_c = pct_change_weeks(gold)

    # 출력
    # 신호등 수집 (MTI 계산용 — 리포트에 표시되는 것과 동일한 값)
    signals = {
        "S&P500":   light_ratio(us_ratio),
        "KOSPI200": light_ratio(kr_ratio),
        "RSP/VOO":  light_change(rsp_voo_4w, good_when_positive=True),
        "KR Equal": light_change(kr_eqw_cap_4w, good_when_positive=True),
        "KOSPI":    light_change(ks_4w, True),
        "KOSDAQ":   light_change(kq_4w, True),
        "VIX":      light_change(vix_4w, good_when_positive=False),
    }
    mti, state, posture = compute_market_temperature(signals)
    bars = int(round((mti + 1) * 5))

    # 출력
    L=[]
    L.append(f"[Market Monitor] {ts}\n")
    L.append(f"Market Temperature: 🌡️ {'▓' * bars}{'░' * (10 - bars)} {int(round((mti+1)*50))}/100")
    L.append(f"상태: {state}")
    L.append(f"포지셔닝: {posture}")
    mti_avg, n_days = update_mti_history(mti, now_utc.astimezone(KST).strftime("%Y-%m-%d"))
    if n_days >= 2:
        L.append(f"최근 {n_days}일 평균 MTI: {mti_avg:+.2f}")
    L.append("")
    L.append("광범위 지표(200일선 상단 비율):")
    L.append(f"  · S&P500: { _pct(us_ratio) if not np.isnan(us_ratio) else 'N/A' } {light_ratio(us_ratio)}")
    L.append(f"  · KOSPI200: { _pct(kr_ratio) if not np.isnan(kr_ratio) else 'N/A' } {light_ratio(kr_ratio)}\n")

    L.append("Equal vs Cap (4주 변화):")
    L.append(f"  · 미국 RSP/VOO: { _pct(rsp_voo_4w) if not np.isnan(rsp_voo_4w) else 'N/A' } {light_change(rsp_voo_4w, good_when_positive=True)}")
    L.append(f"  · 한국 {KR_EQW_NAME} / {KR_CAP_NAME} (252000/069500): "
             f"{ _pct(kr_eqw_cap_4w) if not np.isnan(kr_eqw_cap_4w) else 'N/A' } {light_change(kr_eqw_cap_4w, good_when_positive=True)}\n")

    L.append("지수/변동성 (4주 변화):")
    L.append(f"  · KOSPI: { _pct(ks_4w) if not np.isnan(ks_4w) else 'N/A' } {light_change(ks_4w, True)}")
    L.append(f"  · KOSDAQ: { _pct(kq_4w) if not np.isnan(kq_4w) else 'N/A' } {light_change(kq_4w, True)}")
    L.append(f"  · VIX: { _pct(vix_4w) if not np.isnan(vix_4w) else 'N/A' } {light_change(vix_4w, good_when_positive=False)}\n")

    L.append("거시/경기 신호:")
    sahm = fred("SAHMCURRENT"); t10y3m = fred("T10Y3M"); t10y2y = fred("T10Y2Y")
    hy   = fred("BAMLH0A0HYM2"); bbb    = fred("BAMLC0A4CBBB")
    L.append(f"  · Sahm gap: { (f'{last(sahm):+.2f}pp') if last(sahm)==last(sahm) else 'N/A' }  (>= +0.50pp 시 침체 신호)")
    L.append(f"  · 장단기금리차 T10Y3M: { (f'{last(t10y3m):.2f}%') if last(t10y3m)==last(t10y3m) else 'N/A' }")
    L.append(f"  · 장단기금리차 T10Y2Y: { (f'{last(t10y2y):.2f}%') if last(t10y2y)==last(t10y2y) else 'N/A' }")
    L.append(f"  · HY OAS: { (f'{last(hy)*100:.0f}bp') if last(hy)==last(hy) else 'N/A' }")
    L.append(f"  · BBB OAS: { (f'{last(bbb)*100:.0f}bp') if last(bbb)==last(bbb) else 'N/A' }\n")

    L.append("금리·달러·원자재 (최근값, 4주 변화):")
    L.append(f"  · UST 10Y: { (f'{last(t10):.2f}%') if last(t10)==last(t10) else 'N/A' } ({ _pct(t10_c) if t10_c==t10_c else 'N/A' })")
    L.append(f"  · UST 2Y:  { (f'{last(t02):.2f}%') if last(t02)==last(t02) else 'N/A' } ({ _pct(t02_c) if t02_c==t02_c else 'N/A' })")
    L.append(f"  · {dxy_label}: { (f'{last(dxy):.2f}') if last(dxy)==last(dxy) else 'N/A' } ({ _pct(dxy_c) if dxy_c==dxy_c else 'N/A' })")
    L.append(f"  · USD/KRW: { (f'{last(usdk):.2f}') if last(usdk)==last(usdk) else 'N/A' } ({ _pct(usdk_c) if usdk_c==usdk_c else 'N/A' })")
    L.append(f"  · WTI(최근월): { (f'{last(wti):.2f}') if last(wti)==last(wti) else 'N/A' } ({ _pct(wti_c) if wti_c==wti_c else 'N/A' })")
    L.append(f"  · Gold(선물): { (f'{last(gold):.2f}') if last(gold)==last(gold) else 'N/A' } ({ _pct(gold_c) if gold_c==gold_c else 'N/A' })")

    return "\n".join(L).strip()


# ---------------- 알림/엔트리 ----------------
def send_notifications(text: str):
    # 환경변수 기반 메일/텔레그램 (기존과 동일)
    try:
        smtp_host = os.environ.get("SMTP_HOST")
        smtp_port = int(os.environ.get("SMTP_PORT","587"))
        smtp_user = os.environ.get("SMTP_USERNAME")
        smtp_pass = os.environ.get("SMTP_PASSWORD")
        email_from= os.environ.get("EMAIL_FROM")
        to_list   = [x.strip() for x in os.environ.get("EMAIL_TO","").split(",") if x.strip()]
        if smtp_host and smtp_user and smtp_pass and email_from and to_list:
            import smtplib
            from email.mime.text import MIMEText
            subj_kst = datetime.now(timezone.utc).astimezone(KST).strftime("%Y-%m-%d %H:%M KST")
            msg = MIMEText(text, _charset="utf-8")
            msg["Subject"] = f"[Market Monitor] {subj_kst}"
            msg["From"] = email_from
            msg["To"]   = ", ".join(to_list)
            with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as s:
                s.starttls(); s.login(smtp_user, smtp_pass); s.sendmail(email_from, to_list, msg.as_string())
    except Exception as e:
        logging.exception(f"Email send failed: {e}")

    try:
        bot = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if bot and chat_id:
            url = f"https://api.telegram.org/bot{bot}/sendMessage"
            _sess().post(url, data={"chat_id": chat_id, "text": text[:4096]}, timeout=10)
    except Exception as e:
        logging.exception(f"Telegram send failed: {e}")

def run_monitor(notify: bool = True):
    text = build_report(datetime.now(timezone.utc))
    if notify:
        send_notifications(text)
    return text

if __name__ == "__main__":
    print(run_monitor())
