"""네트워크 없이 가짜 시세로 리포트 전체 경로를 돌려보는 스모크 테스트."""
import os
import sys
import tempfile

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import monitor as m

idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=320)
rng = np.random.default_rng(0)


def fake_download(tk, **kw):
    tks = [tk] if isinstance(tk, str) else tk
    frames = {}
    for t in tks:
        p = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, len(idx))))
        frames[t] = pd.DataFrame({"Adj Close": p, "Close": p}, index=idx)
    return pd.concat(frames, axis=1)


m.yf.download = fake_download
m.fred = lambda sid, start=None: pd.Series(np.full(len(idx), 3.1), index=idx)
m.get_sp500_tickers = lambda: [f"S{i}" for i in range(120)]
m.get_kospi200_tickers = lambda: [f"{i:06d}.KS" for i in range(200)]

with tempfile.TemporaryDirectory() as d:
    os.environ["MTI_HISTORY_PATH"] = os.path.join(d, "mti.json")
    now = pd.Timestamp.now(tz="UTC").to_pydatetime()
    text = m.build_report(now)
    m.build_report(now)  # 같은 날 두 번 실행해도 이력은 1일

    print(text)
    assert "Market Temperature" in text
    assert "HY OAS: 310bp" in text, "OAS는 %→bp 변환"
    assert "N/A" not in text.split("거시")[0], "가짜 데이터에서 N/A가 나오면 안 됨"
    assert "평균 MTI" not in text, "하루치 이력만 있으면 평균 줄 생략"

# 4주 변화 = 정확히 20거래일 전 대비
s = pd.Series(np.arange(1.0, 31.0))
assert abs(m.pct_change_weeks(s) - (30 / 10 - 1)) < 1e-12

print("\nsmoke test OK")
