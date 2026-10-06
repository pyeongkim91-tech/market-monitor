# monitor.py — Market Monitor 리포트 생성 + 텔레그램 발송 (GitHub Actions에서 실행)
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

# 한국 EW/Cap
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

def change_bp_weeks(s: pd.Series, weeks=W4):
    """금리(%)의 4주 변화를 bp로. 금리 수준의 % 변화는 의미가 약하다."""
    s = s.dropna()
    if len(s) <= weeks: return np.nan
    return (s.iloc[-1] - s.iloc[-weeks-1]) * 100

def asof(s: pd.Series, fmt="%m/%d") -> str:
    s = s.dropna() if s is not None else s
    return f" [{s.index[-1]:{fmt}}]" if s is not None and not s.empty else ""

def to_100(mti: float) -> int:
    """MTI(-1~+1)를 0~100 점수로."""
    return int(round((mti + 1) * 50))

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

# 한국장(09:00~15:30 KST)이 끝나기 전에 실행되면 yfinance는 오늘 날짜로 장중 가격을 준다.
# 실행 시각에 따라 신호가 흔들리지 않도록, 마감 데이터가 확정되기 전에는 오늘 봉을 버린다.
KR_SETTLED = (15, 40)  # (시, 분) KST

def _now_kst() -> datetime:
    return datetime.now(KST)

def _is_kr(tk: str) -> bool:
    return tk in (KOSPI, KOSDAQ) or tk.upper().endswith((".KS", ".KQ"))

def drop_partial_kr_bar(obj):
    """Series/DataFrame에서 아직 마감되지 않은 오늘(KST) 봉을 제거."""
    now = _now_kst()
    if obj is None or len(obj) == 0 or (now.hour, now.minute) >= KR_SETTLED:
        return obj
    idx = pd.DatetimeIndex(obj.index)
    if idx.tz is not None:
        idx = idx.tz_convert("Asia/Seoul").tz_localize(None)
    return obj[idx.normalize() < pd.Timestamp(now.date())]

def yf_series(ticker:str, start=START) -> pd.Series:
    try:
        df = yf.download(ticker, start=start, progress=False, group_by="ticker",
                         threads=False, auto_adjust=False)
        s = pick_close(df, ticker)
        return drop_partial_kr_bar(s) if _is_kr(ticker) else s
    except Exception as e:
        logging.exception(f"yf_series({ticker}) failed: {e}")
        return pd.Series(dtype="float64")

# ---------------- 구성종목 ----------------
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
    pykrx가 환경변수 KRX_ID / KRX_PW 로 KRX에 로그인함."""
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
        logging.info(f"KOSPI200 constituents: {len(tickers)} from KRX")
        return tickers
    tickers = _kospi200_from_wiki()
    if len(tickers) >= KOSPI200_MIN:
        logging.info(f"KOSPI200 constituents: {len(tickers)} from Wikipedia")
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
        if close is not None and any(_is_kr(t) for t in chunk):
            close = drop_partial_kr_bar(close)
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
        if _is_kr(eqw) or _is_kr(cap):
            s_eqw, s_cap = drop_partial_kr_bar(s_eqw), drop_partial_kr_bar(s_cap)
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
        s = pick_close(px, tk) if px is not None else pd.Series(dtype="float64")
        out[tk] = drop_partial_kr_bar(s) if _is_kr(tk) else s
    return out

# ---------------- 매크로(FRED) ----------------
# 공식 API(FRED_API_KEY) 우선. fredgraph.csv는 GitHub Actions에서 응답 없이 멈추는 경우가 있어
# 키가 없을 때만 쓰고, 한 번 실패하면 이번 실행에서는 더 시도하지 않는다.
FRED_TIMEOUT = 10
_fred_csv_down = False

def _to_series(dates, values, name):
    s = pd.Series(pd.to_numeric(pd.Series(values), errors="coerce").values,
                  index=pd.to_datetime(pd.Series(dates)), name=name)
    return s.dropna()

def _fred_api(series_id, start, key):
    for _ in range(2):
        try:
            r = requests.get("https://api.stlouisfed.org/fred/series/observations",
                             params={"series_id": series_id, "api_key": key,
                                     "file_type": "json", "observation_start": start},
                             timeout=FRED_TIMEOUT)
            r.raise_for_status()
            obs = r.json()["observations"]
            return _to_series([o["date"] for o in obs], [o["value"] for o in obs], series_id)
        except Exception as e:
            # 예외 메시지에는 api_key가 든 URL이 포함되므로 타입만 기록
            logging.warning(f"FRED API failed for {series_id}: {type(e).__name__}")
    return pd.Series(dtype="float64")

def _fred_csv(series_id, start):
    global _fred_csv_down
    if _fred_csv_down:
        return pd.Series(dtype="float64")
    try:
        r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv",
                         params={"id": series_id, "cosd": start}, timeout=FRED_TIMEOUT)
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text))
        return _to_series(df.iloc[:, 0], df.iloc[:, 1], series_id)
    except Exception as e:
        _fred_csv_down = True
        logging.warning(f"FRED CSV failed for {series_id} ({type(e).__name__}); skipping remaining CSV requests")
        return pd.Series(dtype="float64")

def fred(series_id, start=START):
    key = os.environ.get("FRED_API_KEY")
    if key:
        s = _fred_api(series_id, start, key)
        if not s.empty:
            return s
    return _fred_csv(series_id, start)

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
# GitHub Actions에서는 actions/cache로 MTI_HISTORY_PATH 파일을 실행 간에 이어받음.
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

    t10_c  = change_bp_weeks(t10)
    t02_c  = change_bp_weeks(t02)
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
    voo = idx.get(US_CAP)
    L.append(f"[Market Monitor] {ts}")
    L.append(f"기준: 미국{asof(voo)} 종가 · 한국{asof(ks11)} 종가\n")
    L.append(f"Market Temperature: 🌡️ {'▓' * bars}{'░' * (10 - bars)} {to_100(mti)}/100")
    L.append(f"상태: {state}")
    L.append(f"포지셔닝: {posture}")
    mti_avg, n_days = update_mti_history(mti, now_utc.astimezone(KST).strftime("%Y-%m-%d"))
    if n_days >= 2:
        L.append(f"최근 {n_days}일 평균: {to_100(mti_avg)}/100")
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
    L.append(f"  · Sahm gap: { (f'{last(sahm):+.2f}pp') if last(sahm)==last(sahm) else 'N/A' }{asof(sahm, '%Y-%m')}  (>= +0.50pp 시 침체 신호)")
    L.append(f"  · 장단기금리차 T10Y3M: { (f'{last(t10y3m):.2f}%') if last(t10y3m)==last(t10y3m) else 'N/A' }{asof(t10y3m)}")
    L.append(f"  · 장단기금리차 T10Y2Y: { (f'{last(t10y2y):.2f}%') if last(t10y2y)==last(t10y2y) else 'N/A' }{asof(t10y2y)}")
    L.append(f"  · HY OAS: { (f'{last(hy)*100:.0f}bp') if last(hy)==last(hy) else 'N/A' }{asof(hy)}")
    L.append(f"  · BBB OAS: { (f'{last(bbb)*100:.0f}bp') if last(bbb)==last(bbb) else 'N/A' }{asof(bbb)}\n")

    L.append("금리·달러·원자재 (최근값, 4주 변화):")
    L.append(f"  · UST 10Y: { (f'{last(t10):.2f}%') if last(t10)==last(t10) else 'N/A' } ({ f'{t10_c:+.0f}bp' if t10_c==t10_c else 'N/A' }){asof(t10)}")
    L.append(f"  · UST 2Y:  { (f'{last(t02):.2f}%') if last(t02)==last(t02) else 'N/A' } ({ f'{t02_c:+.0f}bp' if t02_c==t02_c else 'N/A' }){asof(t02)}")
    L.append(f"  · {dxy_label}: { (f'{last(dxy):.2f}') if last(dxy)==last(dxy) else 'N/A' } ({ _pct(dxy_c) if dxy_c==dxy_c else 'N/A' }){asof(dxy)}")
    L.append(f"  · USD/KRW: { (f'{last(usdk):.2f}') if last(usdk)==last(usdk) else 'N/A' } ({ _pct(usdk_c) if usdk_c==usdk_c else 'N/A' }){asof(usdk)}")
    L.append(f"  · WTI(최근월): { (f'{last(wti):.2f}') if last(wti)==last(wti) else 'N/A' } ({ _pct(wti_c) if wti_c==wti_c else 'N/A' }){asof(wti)}")
    L.append(f"  · Gold(선물): { (f'{last(gold):.2f}') if last(gold)==last(gold) else 'N/A' } ({ _pct(gold_c) if gold_c==gold_c else 'N/A' }){asof(gold)}")

    return "\n".join(L).strip()


# ---------------- 알림/엔트리 ----------------
def send_telegram(text: str) -> bool:
    """TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 로 발송. 성공 여부 반환."""
    bot = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (bot and chat_id):
        logging.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
        return False
    try:
        r = _sess().post(f"https://api.telegram.org/bot{bot}/sendMessage",
                         data={"chat_id": chat_id, "text": text[:4096]}, timeout=10)
        if not r.ok:
            # 응답 본문에 토큰이 들어가지 않으므로 그대로 기록
            logging.error(f"Telegram send failed: {r.status_code} {r.text[:200]}")
        return r.ok
    except requests.RequestException as e:
        logging.error(f"Telegram send failed: {type(e).__name__}")  # 예외 메시지엔 URL(토큰) 포함
        return False

def main() -> int:
    text = build_report(datetime.now(timezone.utc))
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"```\n{text}\n```\n")
    if os.environ.get("NOTIFY", "true").lower() == "false":
        logging.info("NOTIFY=false; skipping Telegram")
        return 0
    return 0 if send_telegram(text) else 1

if __name__ == "__main__":
    raise SystemExit(main())
