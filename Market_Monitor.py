# ...existing code...
import os
import logging
from io import StringIO
from datetime import datetime, timezone, timedelta
from functools import lru_cache

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
W4 = 20  # 거래일 기준 4주

US_EQW = "RSP"           # S&P500 equal weight
US_CAP = "VOO"           # S&P500 cap weight
KR_EQW = "252000.KS"     # KODEX200 Equal Weight
KR_CAP = "069500.KS"     # KODEX200
KOSPI  = "^KS11"
KOSDAQ = "^KQ11"
VIX    = "^VIX"

logging.basicConfig(level=logging.INFO)

# ---------------- HTTP 세션(재시도) ----------------
_default_session = None

def _get_session():
    global _default_session
    if _default_session is None:
        session = requests.Session()
        retries = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retries)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0",
            "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
        })
        _default_session = session
    return _default_session

def _http_get(url: str) -> str:
    r = _get_session().get(url, timeout=15)
    r.raise_for_status()
    return r.text

def _pct(x):
    try:
        return f"{x*100:.1f}%"
    except Exception:
        return "N/A"

def _pick_price(df, tk):
    """yfinance download 결과에서 단일 티커 종가 Series 안전 취득"""
    if df is None or len(df) == 0:
        return None
    try:
        if isinstance(df.columns, pd.MultiIndex):
            if tk in df.columns.get_level_values(0):
                sub = df[tk]
                s = None
                if "Adj Close" in sub and not sub["Adj Close"].dropna().empty:
                    s = sub["Adj Close"]
                elif "Close" in sub and not sub["Close"].dropna().empty:
                    s = sub["Close"]
                return s.rename(tk) if s is not None else None
        else:
            # 단일 프레임 형태
            if "Adj Close" in df:
                s = df["Adj Close"].dropna(how="all")
                if tk in s.columns:
                    return s[tk].rename(tk)
            if "Close" in df:
                s = df["Close"].dropna(how="all")
                if tk in s.columns:
                    return s[tk].rename(tk)
    except Exception:
        pass
    return None

def yf_close_series(ticker, start=START):
    """단일 티커 종가 Series (Adj Close 우선)"""
    try:
        df = yf.download(ticker, start=start, progress=False, group_by="ticker", threads=False, auto_adjust=False)
        if isinstance(df, pd.DataFrame):
            s = df.get("Adj Close", None)
            if s is None:
                s = df.get("Close", None)
            if s is None:
                # 단일 시리즈로 내려오는 케이스 방어
                if "Adj Close" in df.columns or "Close" in df.columns:
                    col = "Adj Close" if "Adj Close" in df.columns else "Close"
                    return df[col].dropna()
                return pd.Series(dtype="float64")
            if isinstance(s, pd.DataFrame):
                s = s.iloc[:, 0]
            return s.dropna()
    except Exception as e:
        logging.exception(f"yf_close_series failed: {ticker}: {e}")
    return pd.Series(dtype="float64")

def normalize_yield_pct(s: pd.Series) -> pd.Series:
    """미국채 지수형 티커(^TNX, ^UST2Y)가 10배 스케일로 들어오는 경우 보정"""
    try:
        s = s.dropna()
        if s.empty:
            return s
        if s.iloc[-1] > 20.0:
            return s / 10.0
        return s
    except Exception:
        return s

def pct_change_weeks(s: pd.Series, weeks=W4):
    """최근값 대비 weeks 영업일 전 대비 변화율"""
    try:
        s = s.dropna()
        if len(s) <= weeks:
            return np.nan
        return s.iloc[-1] / s.iloc[-weeks] - 1.0
    except Exception:
        return np.nan

# ---------------- 신호등(리스크 라이트) ----------------
def _lamp(color: str) -> str:
    return {"green":"🟢","yellow":"🟡","orange":"🟠","red":"🔴","off":"⚪"}.get(color,"⚪")

def lamp_breadth(ratio: float) -> str:
    # 200MA 상단 비율: 낮을수록 위험
    if ratio != ratio: return _lamp("off")
    if ratio < 0.35: return _lamp("red")
    if ratio < 0.50: return _lamp("orange")
    if ratio < 0.70: return _lamp("yellow")
    return _lamp("green")

def lamp_eqw_cap_4w(ch: float) -> str:
    # Equal/Cap 4주 변화: 음수로 갈수록 '소수 리더 장세' → 위험
    if ch != ch: return _lamp("off")
    if ch <= -0.02: return _lamp("red")
    if ch <= -0.01: return _lamp("orange")
    if ch < 0.0:    return _lamp("yellow")
    return _lamp("green")

def lamp_index_4w(ch: float) -> str:
    # 지수 4주 변화: 급락 위험 / 과열 위험 양쪽 감지
    if ch != ch: return _lamp("off")
    if ch <= -0.05: return _lamp("red")
    if ch <= -0.02: return _lamp("orange")
    if ch <= 0.06:  return _lamp("green")   # -2%~+6%를 건강 구간으로
    if ch <= 0.10:  return _lamp("orange")  # 과열 경계
    return _lamp("red")                      # 과열 심화

def lamp_vix_4w(ch: float) -> str:
    # VIX 상승은 위험, 하락은 안정
    if ch != ch: return _lamp("off")
    if ch >= 0.20: return _lamp("red")
    if ch >= 0.10: return _lamp("orange")
    if ch <= -0.10: return _lamp("green")
    return _lamp("yellow")

def lamp_sahm(val: float) -> str:
    if val != val: return _lamp("off")
    if val >= 0.50: return _lamp("red")
    if val >= 0.20: return _lamp("yellow")
    return _lamp("green")

def lamp_curve_spread(spread: float) -> str:
    # T10Y3M, T10Y2Y: 음수면 역전(위험)
    if spread != spread: return _lamp("off")
    if spread < 0.0:  return _lamp("red")
    if spread < 0.50: return _lamp("orange")
    if spread < 1.00: return _lamp("yellow")
    return _lamp("green")

def lamp_hy_oas(val: float) -> str:
    # FRED 값은 % 단위. 6%+ 심각, 4~6 경계
    if val != val: return _lamp("off")
    if val >= 6.0: return _lamp("red")
    if val >= 4.0: return _lamp("orange")
    return _lamp("green")

def lamp_bbb_oas(val: float) -> str:
    if val != val: return _lamp("off")
    if val >= 2.5: return _lamp("red")
    if val >= 1.8: return _lamp("orange")
    return _lamp("green")

def lamp_yield_level(level: float, tenor: str) -> str:
    # 레벨 기반 힌트(완벽한 경제학은 아님. 튜닝 가능)
    if level != level: return _lamp("off")
    if tenor == "10Y":
        if level >= 5.0: return _lamp("red")
        if level >= 4.0: return _lamp("orange")
        if level >= 3.0: return _lamp("yellow")
        return _lamp("green")
    if tenor == "2Y":
        if level >= 5.5: return _lamp("red")
        if level >= 4.5: return _lamp("orange")
        if level >= 3.5: return _lamp("yellow")
        return _lamp("green")
    return _lamp("off")

def lamp_pct_move(ch: float, sense: str = "tightening") -> str:
    # DXY/USDKRW/WTI 등 4주 변화율 신호
    if ch != ch: return _lamp("off")
    # 'tightening' = 상승이 긴축/물가압력 → 위험
    if sense == "tightening":
        if ch >= 0.10: return _lamp("red")
        if ch >= 0.05: return _lamp("orange")
        if ch >= 0.01: return _lamp("yellow")
        if ch <= -0.05: return _lamp("green")
        return _lamp("green")
    # gold: 상승은 리스크온/오프 해석 다양 → 변동 과도시 경계
    if sense == "gold":
        if abs(ch) >= 0.10: return _lamp("red")
        if abs(ch) >= 0.05: return _lamp("orange")
        return _lamp("green")
    return _lamp("off")

# ---------------- 지표 소스(FRED 일부 유지) ----------------
def fred(series_id, start=START):
    """FRED 시계열 (안되면 빈 시리즈)"""
    try:
        s = pdr.DataReader(series_id, "fred", start=start)
        if isinstance(s, pd.DataFrame) and s.shape[1] == 1:
            s = s.iloc[:, 0]
        return s
    except Exception as e:
        logging.exception(f"FRED fetch failed for {series_id}: {e}")
        return pd.Series(dtype="float64")

# ---------------- 종목 리스트 ----------------
@lru_cache(maxsize=1)
def get_sp500_tickers():
    """S&P500 구성종목 위키 크롤링 (두 경로 시도), 결과 캐시"""
    urls = [
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        "https://en.wikipedia.org/wiki/List_of_S&P_500_companies",
    ]
    for url in urls:
        try:
            html = _http_get(url)
            soup = BeautifulSoup(html, "html.parser")
            table = soup.find("table", {"id": "constituents"}) or soup.find("table")
            if not table:
                continue
            df = pd.read_html(StringIO(str(table)))[0]
            # 보편적 컬럼명: Symbol
            if "Symbol" in df.columns:
                syms = df["Symbol"].astype(str).str.strip().tolist()
            elif "Ticker symbol" in df.columns:
                syms = df["Ticker symbol"].astype(str).str.strip().tolist()
            else:
                continue
            # yfinance 호환을 위해 '.' -> '-' 변환 (BRK.B 등)
            return [s.replace(".", "-") for s in syms if isinstance(s, str) and s.strip()]
        except Exception as e:
            logging.exception(f"S&P500 ticker fetch failed ({url}): {e}")
    return []

def _normalize_kospi_code(s: str) -> str:
    """숫자 6자리면 '.KS' 접미사 추가, 이미 접미사 있으면 그대로"""
    if not isinstance(s, str):
        return s
    s = s.strip()
    if s == "":
        return s
    if s.upper().endswith(".KS") or s.upper().endswith(".KQ"):
        return s
    if s.isdigit() and len(s) == 6:
        return s + ".KS"
    return s

@lru_cache(maxsize=1)
def get_kospi200_tickers():
    """KOSPI200 위키 크롤링 (형식 다양, 가능한 컬럼 탐색) - 결과 캐시 및 KRX 접미사 보정"""
    try:
        html = _http_get("https://en.wikipedia.org/wiki/KOSPI_200")
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        if not table:
            return []
        df = pd.read_html(StringIO(str(table)))[0]
        for col in ["Ticker", "Code", "Symbol"]:
            if col in df.columns:
                syms = df[col].astype(str).str.strip().tolist()
                return [ _normalize_kospi_code(s) for s in syms if s ]
    except Exception as e:
        logging.exception(f"KOSPI200 ticker fetch failed: {e}")
    return []

# ---------------- 브레드스 계산 ----------------
def breadth_last_series_batched(tickers, start=START, sma_win=SMA_WIN, batch=80):
    """대량 티커를 배치로 받아 마지막 시점에서 종가>SMA200 비율 계산"""
    sum_above = None
    sum_valid = None

    for i in range(0, len(tickers), batch):
        chunk = [t for t in tickers[i:i+batch] if isinstance(t, str) and t.strip()]
        if not chunk:
            continue
        try:
            px = yf.download(chunk, start=start, progress=False, group_by="ticker",
                             threads=False, auto_adjust=False)
        except Exception as e:
            logging.exception(f"yfinance download failed for chunk[{i}:{i+batch}]: {e}")
            continue

        # 종가 매트릭스 만들기
        if isinstance(px.columns, pd.MultiIndex):
            cols = []
            for t in chunk:
                if t in px.columns.get_level_values(0):
                    sub = px[t]
                    s = None
                    if "Adj Close" in sub and not sub["Adj Close"].dropna().empty:
                        s = sub["Adj Close"]
                    elif "Close" in sub and not sub["Close"].dropna().empty:
                        s = sub["Close"]
                    if s is not None:
                        cols.append(s.rename(t))
            close = pd.concat(cols, axis=1) if cols else None
        else:
            # 비멀티 인덱스 방어
            if "Adj Close" in px:
                close = px["Adj Close"].dropna(how="all")
            elif "Close" in px:
                close = px["Close"].dropna(how="all")
            else:
                close = None

        if close is None or close.empty:
            continue

        sma = close.rolling(sma_win, min_periods=sma_win // 2).mean()
        last_close = close.iloc[-1]
        last_sma = sma.iloc[-1]
        above = (last_close > last_sma).astype("int32")
        valid = (~pd.isna(last_close)).astype("int32")

        sum_above = above if sum_above is None else sum_above.add(above, fill_value=0)
        sum_valid = valid if sum_valid is None else sum_valid.add(valid, fill_value=0)

    if sum_valid is None or sum_valid.sum() == 0:
        return pd.Series(dtype="float64")

    return (sum_above / sum_valid).astype("float64")

# ---------------- 지수/ETF 지표 ----------------
def safe_ratio_eqw_cap(eqw=US_EQW, cap=US_CAP, start=START):
    try:
        df = yf.download([eqw, cap], start=start, progress=False, group_by="ticker",
                         threads=False, auto_adjust=False)
        s_eqw = _pick_price(df, eqw)
        s_cap = _pick_price(df, cap)
        if s_eqw is None or s_cap is None:
            return pd.Series(dtype="float64")
        return (s_eqw / s_cap).dropna()
    except Exception as e:
        logging.exception(f"safe_ratio_eqw_cap failed: {e}")
        return pd.Series(dtype="float64")

def get_indices(start=START):
    out = {}
    try:
        px = yf.download([KOSPI, KOSDAQ, VIX, US_EQW, US_CAP], start=start,
                         progress=False, threads=False, group_by="ticker",
                         auto_adjust=False)
    except Exception as e:
        logging.exception(f"index download failed: {e}")
        px = None
    for tk in [KOSPI, KOSDAQ, VIX, US_EQW, US_CAP]:
        out[tk] = _pick_price(px, tk)
    return out

# ---------------- 리포트 ----------------
def build_report(now_utc: datetime):
    # 한국시간으로 표기
    kst = timezone(timedelta(hours=9))
    ts = now_utc.astimezone(kst).strftime("%Y-%m-%d %H:%M KST")

    # 1) 광범위 지표
    spx_members = get_sp500_tickers()
    kospi200_members = get_kospi200_tickers()

    us_breadth = breadth_last_series_batched(spx_members, start=START, sma_win=SMA_WIN, batch=80) if spx_members else pd.Series(dtype="float64")
    kr_breadth = breadth_last_series_batched(kospi200_members, start=START, sma_win=SMA_WIN, batch=80) if kospi200_members else pd.Series(dtype="float64")

    us_ratio = (us_breadth.sum() / len(us_breadth)) if len(us_breadth) > 0 else np.nan
    kr_ratio = (kr_breadth.sum() / len(kr_breadth)) if len(kr_breadth) > 0 else np.nan

    # 2) Equal vs Cap ratio (4주)
    rsp_voo = safe_ratio_eqw_cap(US_EQW, US_CAP, start=START)
    rsp_voo_4w = (rsp_voo.iloc[-1] / rsp_voo.iloc[-W4] - 1.0) if len(rsp_voo) > W4 else np.nan

    kr_eqw_cap = safe_ratio_eqw_cap(KR_EQW, KR_CAP, start=START)
    kr_eqw_cap_4w = (kr_eqw_cap.iloc[-1] / kr_eqw_cap.iloc[-W4] - 1.0) if len(kr_eqw_cap) > W4 else np.nan

    # 3) 지수/변동성 (4주)
    idx = get_indices(START)
    ks11 = idx.get(KOSPI)
    kq11 = idx.get(KOSDAQ)
    vix  = idx.get(VIX)

    ks_4w = (ks11.iloc[-1] / ks11.iloc[-W4] - 1.0) if ks11 is not None and len(ks11) > W4 else np.nan
    kq_4w = (kq11.iloc[-1] / kq11.iloc[-W4] - 1.0) if kq11 is not None and len(kq11) > W4 else np.nan
    vix_4w = (vix.iloc[-1] / vix.iloc[-W4] - 1.0) if vix is not None and len(vix) > W4 else np.nan

    # 4) 금리·달러·원자재 (yfinance로 전환)
    tnx = normalize_yield_pct(yf_close_series("^TNX", START))         # 10Y
    ust2 = normalize_yield_pct(yf_close_series("^UST2Y", START))      # 2Y
    if ust2.empty:
        alt = normalize_yield_pct(yf_close_series("^IRX", START))     # 13-week T-bill (fallback)
        ust2 = alt

    dxy = yf_close_series("DX-Y.NYB", START)
    if dxy.empty:
        dxy = yf_close_series("DX=F", START)                          # 대체

    usdk = yf_close_series("KRW=X", START)
    wti  = yf_close_series("CL=F", START)
    gold = yf_close_series("GC=F", START)

    tnx_ch  = pct_change_weeks(tnx)
    ust2_ch = pct_change_weeks(ust2)
    dxy_ch  = pct_change_weeks(dxy)
    usdk_ch = pct_change_weeks(usdk)
    wti_ch  = pct_change_weeks(wti)
    gold_ch = pct_change_weeks(gold)

    def _fmt_pct_or_na(x): return _pct(x) if x == x and not np.isnan(x) else "N/A"

    # 5) 거시/경기 신호 (FRED 유지)
    sahm = fred("SAHMCURRENT", START)  # Sahm rule gap
    t10y3m = fred("T10Y3M", START)
    t10y2y = fred("T10Y2Y", START)
    hy_oas = fred("BAMLH0A0HYM2", START)  # ICE BofA US High Yield OAS
    bbb_oas = fred("BAMLC0A4CBBB", START) # ICE BofA BBB OAS

    sahm_last = sahm.dropna().iloc[-1] if not sahm.empty else np.nan
    t10y3m_last = t10y3m.dropna().iloc[-1] if not t10y3m.empty else np.nan
    t10y2y_last = t10y2y.dropna().iloc[-1] if not t10y2y.empty else np.nan
    hy_last = hy_oas.dropna().iloc[-1] if not hy_oas.empty else np.nan
    bbb_last = bbb_oas.dropna().iloc[-1] if not bbb_oas.empty else np.nan

    # 출력
    lines = []
    lines.append(f"[Market Monitor] {ts}")
    lines.append("")
    lines.append("광범위 지표(200일선 상단 비율):")
    lines.append(f"  · S&P500: { _pct(us_ratio) if not np.isnan(us_ratio) else 'N/A' }")
    lines.append(f"  · KOSPI200: { _pct(kr_ratio) if not np.isnan(kr_ratio) else 'N/A' }")
    lines.append("")
    lines.append("Equal vs Cap (4주 변화):")
    lines.append(f"  · 미국 RSP/VOO: { _pct(rsp_voo_4w) if not np.isnan(rsp_voo_4w) else 'N/A' }")
    lines.append(f"  · 한국 KODEX200 Equal Weight / KODEX200 (252000/069500): { _pct(kr_eqw_cap_4w) if not np.isnan(kr_eqw_cap_4w) else 'N/A' }")
    lines.append("")
    lines.append("지수/변동성 (4주 변화):")
    lines.append(f"  · KOSPI: { _pct(ks_4w) if not np.isnan(ks_4w) else 'N/A' }")
    lines.append(f"  · KOSDAQ: { _pct(kq_4w) if not np.isnan(kq_4w) else 'N/A' }")
    lines.append(f"  · VIX: { _pct(vix_4w) if not np.isnan(vix_4w) else 'N/A' }")
    lines.append("")
    lines.append("거시/경기 신호:")
    lines.append(f"  · Sahm gap: { (f'{sahm_last:+.2f}pp') if sahm_last==sahm_last else 'N/A' }  (>= +0.50pp 시 침체 신호)")
    lines.append(f"  · 장단기금리차 T10Y3M: { (f'{t10y3m_last:.2f}%') if t10y3m_last==t10y3m_last else 'N/A' }")
    lines.append(f"  · 장단기금리차 T10Y2Y: { (f'{t10y2y_last:.2f}%') if t10y2y_last==t10y2y_last else 'N/A' }")
    lines.append(f"  · HY OAS: { (f'{hy_last:.1f}bp') if hy_last==hy_last else 'N/A' }")
    lines.append(f"  · BBB OAS: { (f'{bbb_last:.1f}bp') if bbb_last==bbb_last else 'N/A' }")
    lines.append("")
    lines.append("금리·달러·원자재 (최근값, 4주 변화):")
    lines.append(f"  · UST 10Y: { (f'{tnx.iloc[-1]:.2f}%') if not tnx.empty else 'N/A' } ({_fmt_pct_or_na(tnx_ch)})")
    lines.append(f"  · UST 2Y:  { (f'{ust2.iloc[-1]:.2f}%') if not ust2.empty else 'N/A' } ({_fmt_pct_or_na(ust2_ch)})")
    lines.append(f"  · 달러지수(DXY): { (f'{dxy.iloc[-1]:.2f}') if not dxy.empty else 'N/A' } ({_fmt_pct_or_na(dxy_ch)})")
    lines.append(f"  · USD/KRW: { (f'{usdk.iloc[-1]:.2f}') if not usdk.empty else 'N/A' } ({_fmt_pct_or_na(usdk_ch)})")
    lines.append(f"  · WTI(최근월): { (f'{wti.iloc[-1]:.2f}') if not wti.empty else 'N/A' } ({_fmt_pct_or_na(wti_ch)})")
    lines.append(f"  · Gold(선물): { (f'{gold.iloc[-1]:.2f}') if not gold.empty else 'N/A' } ({_fmt_pct_or_na(gold_ch)})")

    return "\n".join(lines).strip()

# ---------------- 알림/엔트리 ----------------
def send_notifications(text: str):
    # 이메일
    try:
        smtp_host = os.environ.get("SMTP_HOST")
        smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        smtp_user = os.environ.get("SMTP_USERNAME")
        smtp_pass = os.environ.get("SMTP_PASSWORD")
        email_from = os.environ.get("EMAIL_FROM")
        to_raw = os.environ.get("EMAIL_TO", "")
        to_list = [x.strip() for x in to_raw.split(",") if x.strip()]
        if smtp_host and smtp_user and smtp_pass and email_from and to_list:
            import smtplib
            from email.mime.text import MIMEText
            msg = MIMEText(text, _charset="utf-8")
            msg["Subject"] = f"[Market Monitor] {datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9))).strftime('%Y-%m-%d %H:%M KST')}"
            msg["From"] = email_from
            msg["To"] = ", ".join(to_list)
            with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as s:
                s.starttls()
                s.login(smtp_user, smtp_pass)
                s.sendmail(email_from, to_list, msg.as_string())
        else:
            logging.info("Email not sent: SMTP or recipient configuration missing.")
    except Exception as e:
        logging.exception(f"Email send failed: {e}")

    # 텔레그램
    try:
        bot = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if bot and chat_id:
            url = f"https://api.telegram.org/bot{bot}/sendMessage"
            requests.post(url, data={"chat_id": chat_id, "text": text[:4096]}, timeout=10)
        else:
            logging.info("Telegram not sent: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing.")
    except Exception as e:
        logging.exception(f"Telegram send failed: {e}")

def run_monitor():
    text = build_report(datetime.now(timezone.utc))
    send_notifications(text)
    return text

if __name__ == "__main__":
    try:
        print(run_monitor())
    except Exception as e:
        logging.exception(f"monitor run failed: {e}")
# ...existing code...