# Market_Monitor_fix2.py
import os, logging
from io import StringIO
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf
from pandas_datareader import data as pdr
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------- 기본 설정 ----------------
START = "2019-01-01"
SMA_WIN = 200
W4 = 20  # 영업일 4주
US_EQW, US_CAP = "RSP", "VOO"
KR_EQW, KR_CAP = "252000.KS", "069500.KS"
KOSPI, KOSDAQ, VIX = "^KS11", "^KQ11", "^VIX"

# yfinance 과한 ERROR 억제
logging.getLogger("yfinance").setLevel(logging.WARNING)
logging.basicConfig(level=logging.INFO)

# 캐시(마지막 성공값 저장; 위키/네트워크 실패 대비)
CACHE_DIR = Path("./.mm_cache"); CACHE_DIR.mkdir(exist_ok=True)
def _cache_write(name: str, value: float):
    try:
        Path(CACHE_DIR, name).write_text(str(value))
    except Exception: pass
def _cache_read(name: str) -> float | None:
    try:
        return float(Path(CACHE_DIR, name).read_text().strip())
    except Exception:
        return None

# ---------------- HTTP 세션 ----------------
_sess = None
def _session():
    global _sess
    if _sess is None:
        s = requests.Session()
        rty = Retry(total=3, backoff_factor=1, status_forcelist=[429,500,502,503,504])
        ad = HTTPAdapter(max_retries=rty)
        s.mount("https://", ad); s.mount("http://", ad)
        s.headers.update({
            "User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0",
            "Accept-Language":"en-US,en;q=0.9,ko;q=0.8",
        })
        _sess = s
    return _sess

def _http_get(url: str) -> str:
    r = _session().get(url, timeout=15); r.raise_for_status(); return r.text

def _pct(x):
    try: return f"{x*100:.1f}%"
    except: return "N/A"

def _emoji_zone(val, pos_is_hot=True):
    # 값이 클수록 과열(빨강)인지/냉각(초록)인지 방향성 다를 수 있어 간단화
    if val != val or np.isnan(val): return "⚪"
    if pos_is_hot:
        return "🔴" if val > 0.03 else ("🟡" if val > 0.0 else "🟢")
    else:
        return "🔴" if val < -0.03 else ("🟡" if val < 0.0 else "🟢")

def _pick_price(df, tk):
    if df is None or len(df)==0: return None
    try:
        if isinstance(df.columns, pd.MultiIndex):
            if tk in df.columns.get_level_values(0):
                sub = df[tk]
                s = sub.get("Adj Close", None)
                if s is None or s.dropna().empty:
                    s = sub.get("Close", None)
                return s.rename(tk) if s is not None else None
        else:
            if "Adj Close" in df and tk in df["Adj Close"].columns:
                return df["Adj Close"][tk].dropna().rename(tk)
            if "Close" in df and tk in df["Close"].columns:
                return df["Close"][tk].dropna().rename(tk)
    except Exception:
        pass
    return None

def yf_series(ticker, start=START):
    try:
        df = yf.download(ticker, start=start, progress=False, group_by="ticker",
                         threads=False, auto_adjust=False)
        if isinstance(df, pd.Series):
            return df.dropna()
        if isinstance(df, pd.DataFrame):
            s = df.get("Adj Close") if "Adj Close" in df else df.get("Close")
            if s is None:
                return pd.Series(dtype="float64")
            if isinstance(s, pd.DataFrame):
                s = s.iloc[:,0]
            return s.dropna()
    except Exception:
        return pd.Series(dtype="float64")
    return pd.Series(dtype="float64")

def normalize_yield_pct(s: pd.Series) -> pd.Series:
    s = s.dropna()
    if s.empty: return s
    return s/10.0 if s.iloc[-1] > 20.0 else s

def pct_change_weeks(s: pd.Series, weeks=W4):
    try:
        s = s.dropna()
        if len(s) <= weeks: return np.nan
        return s.iloc[-1]/s.iloc[-weeks]-1.0
    except Exception:
        return np.nan

# --------- FRED ---------
def fred(series_id, start=START):
    try:
        s = pdr.DataReader(series_id, "fred", start=start)
        if isinstance(s, pd.DataFrame) and s.shape[1]==1: s = s.iloc[:,0]
        return s.dropna()
    except Exception:
        return pd.Series(dtype="float64")

# --------- 구성종목 ---------
def _norm_kr_code(s: str) -> str:
    if not isinstance(s,str): return s
    s = s.strip()
    if s.endswith((".KS",".KQ")): return s
    return s + ".KS" if s.isdigit() and len(s)==6 else s

@lru_cache(maxsize=1)
def get_sp500():
    urls = [
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        "https://en.wikipedia.org/wiki/List_of_S&P_500_companies",
    ]
    for u in urls:
        try:
            soup = BeautifulSoup(_http_get(u), "html.parser")
            table = soup.find("table", {"id":"constituents"}) or soup.find("table")
            df = pd.read_html(StringIO(str(table)))[0]
            col = "Symbol" if "Symbol" in df.columns else ("Ticker symbol" if "Ticker symbol" in df.columns else None)
            if not col: continue
            return [str(x).strip().replace(".","-") for x in df[col] if isinstance(x,str)]
        except Exception: pass
    return []

@lru_cache(maxsize=1)
def get_kospi200():
    try:
        soup = BeautifulSoup(_http_get("https://en.wikipedia.org/wiki/KOSPI_200"), "html.parser")
        table = soup.find("table")
        df = pd.read_html(StringIO(str(table)))[0]
        for col in ["Ticker","Code","Symbol"]:
            if col in df.columns:
                syms = [ _norm_kr_code(str(x).strip()) for x in df[col] if isinstance(x,str) ]
                return syms
    except Exception: pass
    return []

def breadth_last_series_batched(tickers, start=START, sma_win=SMA_WIN, batch=80):
    sum_above = None; sum_valid = None
    for i in range(0, len(tickers), batch):
        chunk = [t for t in tickers[i:i+batch] if isinstance(t,str) and t]
        if not chunk: continue
        try:
            px = yf.download(chunk, start=start, progress=False, group_by="ticker",
                             threads=False, auto_adjust=False)
        except Exception:
            continue
        if isinstance(px.columns, pd.MultiIndex):
            cols=[]
            for t in chunk:
                if t in px.columns.get_level_values(0):
                    sub = px[t]
                    s = sub.get("Adj Close")
                    if s is None or s.dropna().empty: s = sub.get("Close")
                    if s is not None: cols.append(s.rename(t))
            close = pd.concat(cols, axis=1) if cols else None
        else:
            close = px.get("Adj Close", px.get("Close", None))
            if isinstance(close, pd.DataFrame): close = close.dropna(how="all")
        if close is None or close.empty: continue

        sma = close.rolling(sma_win, min_periods=sma_win//2).mean()
        last_close, last_sma = close.iloc[-1], sma.iloc[-1]
        above = (last_close > last_sma).astype("int32")
        valid = (~pd.isna(last_close)).astype("int32")
        sum_above = above if sum_above is None else sum_above.add(above, fill_value=0)
        sum_valid = valid if sum_valid is None else sum_valid.add(valid, fill_value=0)

    if sum_valid is None or sum_valid.sum()==0: return pd.Series(dtype="float64")
    return (sum_above/sum_valid).astype("float64")

# --------- 지수/ETF ---------
def safe_ratio_eqw_cap(eqw, cap, start=START):
    try:
        df = yf.download([eqw, cap], start=start, progress=False, group_by="ticker",
                         threads=False, auto_adjust=False)
        s_eqw = _pick_price(df, eqw); s_cap = _pick_price(df, cap)
        return (s_eqw/s_cap).dropna() if s_eqw is not None and s_cap is not None else pd.Series(dtype="float64")
    except Exception:
        return pd.Series(dtype="float64")

def get_indices(start=START):
    out={}
    try:
        px = yf.download([KOSPI, KOSDAQ, VIX, US_EQW, US_CAP], start=start,
                         progress=False, threads=False, group_by="ticker", auto_adjust=False)
    except Exception:
        px=None
    for tk in [KOSPI, KOSDAQ, VIX, US_EQW, US_CAP]:
        out[tk]=_pick_price(px, tk)
    return out

# --------- 대체 가능한 시계열 헬퍼 ---------
def series_with_fallback(primary_list, fred_id=None, scale=1.0):
    # primary_list: [("YF","KRW=X"), ("YF","DX-Y.NYB"), ...]
    for src, key in primary_list:
        if src=="YF":
            s = yf_series(key, START)
        else:
            s = pd.Series(dtype="float64")
        if not s.empty: return s*scale
    if fred_id:
        s = fred(fred_id, START)
        if not s.empty: return s*scale
    return pd.Series(dtype="float64")

# --------- 리포트 ---------
def build_report(now_utc: datetime):
    kst = timezone(timedelta(hours=9))
    ts = now_utc.astimezone(kst).strftime("%Y-%m-%d %H:%M KST")

    # (1) 브레드스
    spx = get_sp500(); krx = get_kospi200()
    us_b = breadth_last_series_batched(spx, sma_win=SMA_WIN) if spx else pd.Series(dtype="float64")
    kr_b = breadth_last_series_batched(krx, sma_win=SMA_WIN) if krx else pd.Series(dtype="float64")
    us_ratio = (us_b.sum()/len(us_b)) if len(us_b)>0 else np.nan
    kr_ratio = (kr_b.sum()/len(kr_b)) if len(kr_b)>0 else np.nan

    # 캐시 보정
    if not np.isnan(us_ratio): _cache_write("spx_breadth", us_ratio)
    else:
        c = _cache_read("spx_breadth"); 
        if c is not None: us_ratio=c
    if not np.isnan(kr_ratio): _cache_write("kospi200_breadth", kr_ratio)
    else:
        c = _cache_read("kospi200_breadth"); 
        if c is not None: kr_ratio=c

    # (2) Equal vs Cap
    rsp_voo = safe_ratio_eqw_cap(US_EQW, US_CAP)
    kr_eqw_cap = safe_ratio_eqw_cap(KR_EQW, KR_CAP)
    rsp_voo_4w = (rsp_voo.iloc[-1]/rsp_voo.iloc[-W4]-1) if len(rsp_voo)>W4 else np.nan
    kr_eqw_cap_4w = (kr_eqw_cap.iloc[-1]/kr_eqw_cap.iloc[-W4]-1) if len(kr_eqw_cap)>W4 else np.nan

    # (3) 지수/변동성
    idx = get_indices()
    ks11, kq11, vix = idx.get(KOSPI), idx.get(KOSDAQ), idx.get(VIX)
    ks_4w = (ks11.iloc[-1]/ks11.iloc[-W4]-1) if ks11 is not None and len(ks11)>W4 else np.nan
    kq_4w = (kq11.iloc[-1]/kq11.iloc[-W4]-1) if kq11 is not None and len(kq11)>W4 else np.nan
    vix_4w = (vix.iloc[-1]/vix.iloc[-W4]-1) if vix is not None and len(vix)>W4 else np.nan

    # (4) 금리·달러·원자재 (다중 소스)
    t10 = fred("DGS10"); 
    if t10.empty: t10 = normalize_yield_pct(yf_series("^TNX"))
    t02 = fred("DGS2");  
    if t02.empty: t02 = normalize_yield_pct(yf_series("^FVX"))  # 5Y 대체, 없으면 IRX 등으로 변경 가능

    dxy = series_with_fallback([("YF","DX-Y.NYB"),("YF","DX=F")], fred_id="DTWEXBGS")
    usdk = series_with_fallback([("YF","KRW=X")], fred_id="DEXKOUS")
    # FRED DEXKOUS는 주간 -> 앞으로 채움
    if not usdk.empty: usdk = usdk.asfreq("B").ffill()

    wti  = series_with_fallback([("YF","CL=F")], fred_id="DCOILWTICO")
    gold = series_with_fallback([("YF","GC=F")], fred_id="GOLDAMGBD228NLBM")

    def ch(s): return pct_change_weeks(s)

    t10_c, t02_c = ch(t10), ch(t02)
    dxy_c, usdk_c, wti_c, gold_c = ch(dxy), ch(usdk), ch(wti), ch(gold)

    # (5) 경기 신호(FRED)
    sahm = fred("SAHMCURRENT"); t10y3m = fred("T10Y3M"); t10y2y = fred("T10Y2Y")
    hy = fred("BAMLH0A0HYM2"); bbb = fred("BAMLC0A4CBBB")
    sahm_last = sahm.iloc[-1] if not sahm.empty else np.nan
    t103m_last = t10y3m.iloc[-1] if not t10y3m.empty else np.nan
    t102y_last = t10y2y.iloc[-1] if not t10y2y.empty else np.nan
    hy_last = hy.iloc[-1] if not hy.empty else np.nan
    bbb_last = bbb.iloc[-1] if not bbb.empty else np.nan

    # 출력
    lines=[]
    lines.append(f"[Market Monitor] {ts}\n")
    lines.append("광범위 지표(200일선 상단 비율):")
    lines.append(f"  · S&P500: { _pct(us_ratio) if us_ratio==us_ratio else 'N/A' } {_emoji_zone(us_ratio)}")
    lines.append(f"  · KOSPI200: { _pct(kr_ratio) if kr_ratio==kr_ratio else 'N/A' } {_emoji_zone(kr_ratio)}\n")

    lines.append("Equal vs Cap (4주 변화):")
    lines.append(f"  · 미국 RSP/VOO: { _pct(rsp_voo_4w) if rsp_voo_4w==rsp_voo_4w else 'N/A' } {_emoji_zone(rsp_voo_4w, pos_is_hot=False)}")
    lines.append(f"  · 한국 KODEX200 Equal Weight / KODEX200 (252000/069500): { _pct(kr_eqw_cap_4w) if kr_eqw_cap_4w==kr_eqw_cap_4w else 'N/A' } {_emoji_zone(kr_eqw_cap_4w, pos_is_hot=False)}\n")

    lines.append("지수/변동성 (4주 변화):")
    lines.append(f"  · KOSPI: { _pct(ks_4w) if ks_4w==ks_4w else 'N/A' } {_emoji_zone(ks_4w)}")
    lines.append(f"  · KOSDAQ: { _pct(kq_4w) if kq_4w==kq_4w else 'N/A' } {_emoji_zone(kq_4w)}")
    lines.append(f"  · VIX: { _pct(vix_4w) if vix_4w==vix_4w else 'N/A' } {_emoji_zone(-vix_4w)}\n")

    lines.append("거시/경기 신호:")
    lines.append(f"  · Sahm gap: { (f'{sahm_last:+.2f}pp') if sahm_last==sahm_last else 'N/A' }  (>= +0.50pp 시 침체 신호)")
    lines.append(f"  · 장단기금리차 T10Y3M: { (f'{t103m_last:.2f}%') if t103m_last==t103m_last else 'N/A' }")
    lines.append(f"  · 장단기금리차 T10Y2Y: { (f'{t102y_last:.2f}%') if t102y_last==t102y_last else 'N/A' }")
    lines.append(f"  · HY OAS: { (f'{hy_last:.1f}bp') if hy_last==hy_last else 'N/A' }")
    lines.append(f"  · BBB OAS: { (f'{bbb_last:.1f}bp') if bbb_last==bbb_last else 'N/A' }\n")

    lines.append("금리·달러·원자재 (최근값, 4주 변화):")
    lines.append(f"  · UST 10Y: { (f'{t10.iloc[-1]:.2f}%') if not t10.empty else 'N/A' } ({ _pct(t10_c) if t10_c==t10_c else 'N/A' })")
    lines.append(f"  · UST 2Y:  { (f'{t02.iloc[-1]:.2f}%') if not t02.empty else 'N/A' } ({ _pct(t02_c) if t02_c==t02_c else 'N/A' })")
    lines.append(f"  · 달러지수(DXY/대체): { (f'{dxy.iloc[-1]:.2f}') if not dxy.empty else 'N/A' } ({ _pct(dxy_c) if dxy_c==dxy_c else 'N/A' })")
    lines.append(f"  · USD/KRW: { (f'{usdk.iloc[-1]:.2f}') if not usdk.empty else 'N/A' } ({ _pct(usdk_c) if usdk_c==usdk_c else 'N/A' })")
    lines.append(f"  · WTI(최근월): { (f'{wti.iloc[-1]:.2f}') if not wti.empty else 'N/A' } ({ _pct(wti_c) if wti_c==wti_c else 'N/A' })")
    lines.append(f"  · Gold(선물): { (f'{gold.iloc[-1]:.2f}') if not gold.empty else 'N/A' } ({ _pct(gold_c) if gold_c==gold_c else 'N/A' })")

    return "\n".join(lines)

# 알림/엔트리 포인트는 기존 monitor.py의 것을 그대로 사용 (app.py에서 import monitor 사용)
def send_notifications(_): pass
def run_monitor():
    return build_report(datetime.now(timezone.utc))