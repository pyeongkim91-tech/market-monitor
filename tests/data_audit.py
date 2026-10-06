"""실제 데이터로 리포트 입력값의 기준일·원본값·이상치를 점검한다 (발송 없음).

각 시계열에 대해 마지막 날짜/값, 4주 비교 기준(20거래일 전) 날짜/값, 창 안의 최대 일간 변동을 출력한다.
"""
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import monitor as m

now_kst = datetime.now(timezone.utc).astimezone(m.KST)
print(f"audit run at {now_kst:%Y-%m-%d %H:%M KST}\n")


def show(name, s, unit=""):
    s = s.dropna()
    if s.empty:
        print(f"{name:<14} EMPTY")
        return
    last_d, last_v = s.index[-1], s.iloc[-1]
    line = f"{name:<14} last {last_d:%Y-%m-%d} {last_v:>10.3f}{unit}"
    if len(s) > m.W4:
        base_d, base_v = s.index[-m.W4 - 1], s.iloc[-m.W4 - 1]
        win = s.iloc[-m.W4 - 1:]
        jumps = win.pct_change().abs()
        line += (f" | base {base_d:%Y-%m-%d} {base_v:>10.3f} | 4w {last_v / base_v - 1:+.1%}"
                 f" | max 1d move {jumps.max():.1%} on {jumps.idxmax():%Y-%m-%d}")
    print(line)
    return s


print("== Yahoo ==")
for tk in ["^KS11", "^KQ11", "^VIX", "RSP", "VOO", "252000.KS", "069500.KS",
           "^TNX", "DX-Y.NYB", "KRW=X", "CL=F", "GC=F"]:
    show(tk, m.yf_series(tk))

print("\n== ratios ==")
show("RSP/VOO", m.ratio_eqw_cap(m.US_EQW, m.US_CAP))
show("252000/069500", m.ratio_eqw_cap(m.KR_EQW, m.KR_CAP))

print("\n== FRED ==")
for sid in ["DGS10", "DGS2", "DGS3MO", "T10Y2Y", "T10Y3M", "SAHMCURRENT",
            "BAMLH0A0HYM2", "BAMLC0A4CBBB"]:
    show(sid, m.fred(sid))

print("\n== RSP/VOO last 6 bars ==")
r = m.ratio_eqw_cap(m.US_EQW, m.US_CAP)
print(r.tail(6).round(5).to_string())
