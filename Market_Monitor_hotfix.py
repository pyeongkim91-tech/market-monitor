# Market_Monitor_hotfix.py
import os, logging, re
from io import StringIO
from datetime import datetime, timezone, timedelta
from functools import lru_cache

import numpy as np
import pandas as pd
import yfinance as yf
from pandas_datareader import data as pdr
import requests
from bs4 import BeautifulSoup

START = "2019-01-01"
SMA_WIN = 200
W4 = 20

US_EQW, US_CAP = "RSP", "VOO"
KR_EQW, KR_CAP = "252000.KS", "069500.KS"
KOSPI, KOSDAQ, VIX_TK = "^KS11", "^KQ11", "^VIX"

logging.getLogger("yfinance").setLevel(logging.WARNING)
logging.basicConfig(level=logging.INFO)

# ---------- utils ----------
def _pct(x):
    try: return f"{x*100:.1f}%"
    except: return "N/A"

def _emoji_zone(val, pos_is_hot=True):
    if val!=val or np.isnan(val): return "⚪"
    if pos_is_hot:
        return "🔴" if val>0.03 else ("🟡" if val>0 else "🟢")
    else:
        return "🔴" if val<-0.03 else ("🟡" if val<0 else "🟢")

def pct_change_weeks(s: pd.Series, weeks=W4):
    try:
        s=s.dropna()
        if len(s)<=weeks: return np.nan
        return s.iloc[-1]/s.iloc[-weeks]-1.0
    except: return np.nan

def yf_series(ticker, start=START):
    try:
        df=yf.download(ticker, start=start, progress=False, group_by="ticker",
                       threads=False, auto_adjust=False)
        if isinstance(df, pd.Series): return df.dropna()
        if isinstance(df, pd.DataFrame):
            s=df.get("Adj Close") if "Adj Close" in df else df.get("Close")
            if s is None: return pd.Series(dtype="float64")
            if isinstance(s, pd.DataFrame): s=s.iloc[:,0]
            return s.dropna()
    except: pass
    return pd.Series(dtype="float64")

def fred(series_id, start=START):
    try:
        s=pdr.DataReader(series_id, "fred", start=start)
        if isinstance(s, pd.DataFrame) and s.shape[1]==1: s=s.iloc[:,0]
        return s.dropna()
    except: return pd.Series(dtype="float64")

# ---------- tickers ----------
@lru_cache(maxsize=1)
def get_sp500_tickers():
    urls = [
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        "https://en.wikipedia.org/wiki/List_of_S&P_500_companies",
    ]
    for u in urls:
        try:
            html=requests.get(u, timeout=15).text
            soup=BeautifulSoup(html, "html.parser")
            table=soup.find("table", {"id":"constituents"}) or soup.find("table")
            df=pd.read_html(StringIO(str(table)))[0]
            for col in ["Symbol","Ticker symbol"]:
                if col in df.columns:
                    syms=df[col].astype(str).str.strip().tolist()
                    return [s.replace(".", "-") for s in syms if s]
        except: pass
    return []

def _norm_kr_code(s: str)->str:
    s=str(s).strip()
    if s.endswith((".KS",".KQ")): return s
    return s+".KS" if s.isdigit() and len(s)==6 else s

@lru_cache(maxsize=1)
def get_kospi200_tickers():
    try:
        html=requests.get("https://en.wikipedia.org/wiki/KOSPI_200", timeout=15).text
        tables=pd.read_html(html)
        cand=[]
        for t in tables:
            for col in t.columns:
                seq = t[col].astype(str).tolist()
                for v in seq:
                    m=re.search(r"\b(\d{6})\b", v)
                    if m: cand.append(m.group(1))
        cand=list(dict.fromkeys(cand))  # uniq & keep order
        return [_norm_kr_code(x) for x in cand]
    except: return []

# ---------- breadth ----------
def breadth_last_series_batched(tickers, start=START, sma_win=SMA_WIN, batch=80):
    sum_above=None; sum_valid=None
    for i in range(0, len(tickers), batch):
        chunk=[t for t in tickers[i:i+batch] if t]
        if not chunk: continue
        try:
            px=yf.download(chunk, start=start, progress=False, group_by="ticker",
                           threads=False, auto_adjust=False)
        except: continue
        if isinstance(px.columns, pd.MultiIndex):
            cols=[]
            for t in chunk:
                if t in px.columns.get_level_values(0):
                    sub=px[t]; s=sub.get("Adj Close")
                    if s is None or s.dropna().empty: s=sub.get("Close")
                    if s is not None: cols.append(s.rename(t))
            close=pd.concat(cols, axis=1) if cols else None
        else:
            close=px.get("Adj Close", px.get("Close", None))
            if isinstance(close, pd.DataFrame): close=close.dropna(how="all")
        if close is None or close.empty: continue
        sma=close.rolling(sma_win, min_periods=sma_win//2).mean()
        last_close, last_sma = close.iloc[-1], sma.iloc[-1]
        above=(last_close>last_sma).astype("int32")
        valid=(~pd.isna(last_close)).astype("int32")
        sum_above=above if sum_above is None else sum_above.add(above, fill_value=0)
        sum_valid=valid if sum_valid is None else sum_valid.add(valid, fill_value=0)
    if sum_valid is None or sum_valid.sum()==0: return pd.Series(dtype="float64")
    return (sum_above/sum_valid).astype("float64")

# ---------- ratios & indices ----------
def safe_ratio_eqw_cap(eqw, cap, start=START):
    try:
        df=yf.download([eqw, cap], start=start, progress=False, group_by="ticker",
                       threads=False, auto_adjust=False)
        def _pick(df, tk):
            if isinstance(df.columns, pd.MultiIndex) and tk in df.columns.get_level_values(0):
                sub=df[tk]; s=sub.get("Adj Close", sub.get("Close", None))
                return s if s is None else s.rename(tk)
            for key in ("Adj Close","Close"):
                if key in df and tk in df[key].columns: return df[key][tk].rename(tk)
            return None
        s_eqw=_pick(df, eqw); s_cap=_pick(df, cap)
        return (s_eqw/s_cap).dropna() if s_eqw is not None and s_cap is not None else pd.Series(dtype="float64")
    except: return pd.Series(dtype="float64")

def get_indices(start=START):
    out={}
    try:
        px=yf.download([KOSPI, KOSDAQ, US_EQW, US_CAP], start=start,
                       progress=False, threads=False, group_by="ticker", auto_adjust=False)
    except: px=None
    def _pick(df, tk):
        try:
            if isinstance(df.columns, pd.MultiIndex) and tk in df.columns.get_level_values(0):
                sub=df[tk]; s=sub.get("Adj Close", sub.get("Close", None))
                return s if s is None else s.rename(tk)
            for key in ("Adj Close","Close"):
                if key in df and tk in df[key].columns: return df[key][tk].rename(tk)
        except: pass
        return None
    for tk in [KOSPI, KOSDAQ, US_EQW, US_CAP]:
        out[tk]=_pick(px, tk) if px is not None else None
    # VIX는 단독으로 강제 호출
    v = yf_series(VIX_TK, start)
    if v.empty:
        v = yf_series("VIXY", start)  # 방향성 근사
    out[VIX_TK] = v
    return out

# ---------- report ----------
def build_report(now_utc: datetime):
    kst=timezone(timedelta(hours=9)); ts=now_utc.astimezone(kst).strftime("%Y-%m-%d %H:%M KST")

    # 1) Breadth
    spx = get_sp500_tickers(); krx = get_kospi200_tickers()
    us_b = breadth_last_series_batched(spx) if spx else pd.Series(dtype="float64")
    kr_b = breadth_last_series_batched(krx) if krx else pd.Series(dtype="float64")
    us_ratio = (us_b.sum()/len(us_b)) if len(us_b)>0 else np.nan
    kr_ratio = (kr_b.sum()/len(kr_b)) if len(kr_b)>0 else np.nan
    if kr_ratio!=kr_ratio or np.isnan(kr_ratio):
        kospi200_etf = yf_series(KR_CAP)
        if not kospi200_etf.empty:
            sma = kospi200_etf.rolling(SMA_WIN, min_periods=SMA_WIN//2).mean()
            kr_ratio = float(kospi200_etf.iloc[-1] > sma.iloc[-1])

    # 2) Equal vs Cap
    rsp_voo = safe_ratio_eqw_cap(US_EQW, US_CAP)
    kr_eqw_cap = safe_ratio_eqw_cap(KR_EQW, KR_CAP)
    rsp_voo_4w = (rsp_voo.iloc[-1]/rsp_voo.iloc[-W4]-1) if len(rsp_voo)>W4 else np.nan
    kr_eqw_cap_4w = (kr_eqw_cap.iloc[-1]/kr_eqw_cap.iloc[-W4]-1) if len(kr_eqw_cap)>W4 else np.nan

    # 3) Indices / Vol
    idx = get_indices()
    ks11, kq11, vix = idx.get(KOSPI), idx.get(KOSDAQ), idx.get(VIX_TK)
    ks_4w = (ks11.iloc[-1]/ks11.iloc[-W4]-1) if ks11 is not None and len(ks11)>W4 else np.nan
    kq_4w = (kq11.iloc[-1]/kq11.iloc[-W4]-1) if kq11 is not None and len(kq11)>W4 else np.nan
    vix_4w = (vix.iloc[-1]/vix.iloc[-W4]-1) if vix is not None and len(vix)>W4 else np.nan

    # 4) Rates/FX/Commodities (FRED 1순위로 안정화)
    t10 = fred("DGS10");  t02 = fred("DGS2")
    dxy = yf_series("DX-Y.NYB")
    if dxy.empty: dxy = fred("DTWEXBGS")
    usdk = yf_series("KRW=X"); 
    if usdk.empty: usdk = fred("DEXKOUS").asfreq("B").ffill()
    wti = yf_series("CL=F"); 
    if wti.empty: wti = fred("DCOILWTICO")
    gold = yf_series("GC=F"); 
    if gold.empty: gold = fred("GOLDAMGBD228NLBM")

    t10_c, t02_c = pct_change_weeks(t10), pct_change_weeks(t02)
    dxy_c, usdk_c, wti_c, gold_c = pct_change_weeks(dxy), pct_change_weeks(usdk), pct_change_weeks(wti), pct_change_weeks(gold)

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
    sahm=fred("SAHMCURRENT"); t10y3m=fred("T10Y3M"); t10y2y=fred("T10Y2Y")
    hy=fred("BAMLH0A0HYM2"); bbb=fred("BAMLC0A4CBBB")
    def last(s): return s.iloc[-1] if not s.empty else np.nan
    lines.append(f"  · Sahm gap: { (f'{last(sahm):+.2f}pp') if last(sahm)==last(sahm) else 'N/A' }  (>= +0.50pp 시 침체 신호)")
    lines.append(f"  · 장단기금리차 T10Y3M: { (f'{last(t10y3m):.2f}%') if last(t10y3m)==last(t10y3m) else 'N/A' }")
    lines.append(f"  · 장단기금리차 T10Y2Y: { (f'{last(t10y2y):.2f}%') if last(t10y2y)==last(t10y2y) else 'N/A' }")
    lines.append(f"  · HY OAS: { (f'{last(hy):.1f}bp') if last(hy)==last(hy) else 'N/A' }")
    lines.append(f"  · BBB OAS: { (f'{last(bbb):.1f}bp') if last(bbb)==last(bbb) else 'N/A' }\n")
    lines.append("금리·달러·원자재 (최근값, 4주 변화):")
    lines.append(f"  · UST 10Y: { (f'{last(t10):.2f}%') if not t10.empty else 'N/A' } ({ _pct(t10_c) if t10_c==t10_c else 'N/A' })")
    lines.append(f"  · UST 2Y:  { (f'{last(t02):.2f}%') if not t02.empty else 'N/A' } ({ _pct(t02_c) if t02_c==t02_c else 'N/A' })")
    lines.append(f"  · 달러지수(DXY/대체): { (f'{last(dxy):.2f}') if not dxy.empty else 'N/A' } ({ _pct(dxy_c) if dxy_c==dxy_c else 'N/A' })")
    lines.append(f"  · USD/KRW: { (f'{last(usdk):.2f}') if not usdk.empty else 'N/A' } ({ _pct(usdk_c) if usdk_c==usdk_c else 'N/A' })")
    lines.append(f"  · WTI(최근월): { (f'{last(wti):.2f}') if not wti.empty else 'N/A' } ({ _pct(wti_c) if wti_c==wti_c else 'N/A' })")
    lines.append(f"  · Gold(선물): { (f'{last(gold):.2f}') if not gold.empty else 'N/A' } ({ _pct(gold_c) if gold_c==gold_c else 'N/A' })")
    return "\n".join(lines)

def send_notifications(_): pass
def run_monitor(): return build_report(datetime.now(timezone.utc))