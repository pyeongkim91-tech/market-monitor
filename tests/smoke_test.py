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
    assert "일 평균" not in text, "하루치 이력만 있으면 평균 줄 생략"
    assert "UST 2Y:  3.10% (+0bp)" in text, "금리 4주 변화는 bp"
    assert "기준: 미국 [" in text and "HY OAS: 310bp [" in text, "기준일 표기"

    # 이튿날 실행하면 평균이 0~100 척도로 붙는다
    text2 = m.build_report(now + pd.Timedelta(days=1).to_pytimedelta())
    assert "최근 2일 평균: " in text2 and "/100" in text2.split("최근 2일 평균: ")[1].split("\n")[0]

# 4주 변화 = 정확히 20거래일 전 대비
s = pd.Series(np.arange(1.0, 31.0))
assert abs(m.pct_change_weeks(s) - (30 / 10 - 1)) < 1e-12

# 한국장 마감 전에는 오늘 봉을 버리고, 마감 후에는 유지
from datetime import datetime as _dt
kr = pd.Series([1.0, 2.0, 3.0], index=pd.to_datetime(["2026-09-29", "2026-09-30", "2026-10-01"]))
m._now_kst = lambda: _dt(2026, 10, 1, 10, 37, tzinfo=m.KST)
assert list(m.drop_partial_kr_bar(kr).values) == [1.0, 2.0]
m._now_kst = lambda: _dt(2026, 10, 1, 16, 0, tzinfo=m.KST)
assert list(m.drop_partial_kr_bar(kr).values) == [1.0, 2.0, 3.0]
m._now_kst = lambda: _dt(2026, 10, 2, 7, 50, tzinfo=m.KST)  # 다음날 아침: 어제 봉은 확정치
assert list(m.drop_partial_kr_bar(kr).values) == [1.0, 2.0, 3.0]
assert m._is_kr("069500.KS") and m._is_kr("^KS11") and not m._is_kr("KRW=X") and not m._is_kr("RSP")

# bp 변화: 4.75% -> 5.26% = +51bp
y = pd.Series(np.r_[4.75, np.full(19, 5.0), 5.26])
assert round(m.change_bp_weeks(y)) == 51

print("\nsmoke test OK")

# FRED: API 응답 파싱('.'은 결측) + CSV 실패 시 이후 요청 건너뜀
import importlib
m = importlib.reload(m)


class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


calls = []


def fake_get(url, params=None, timeout=None):
    calls.append(url)
    if "api.stlouisfed.org" in url:
        return _Resp({"observations": [{"date": "2026-01-02", "value": "3.10"},
                                       {"date": "2026-01-05", "value": "."}]})
    raise m.requests.ConnectTimeout("boom")


m.requests.get = fake_get
os.environ["FRED_API_KEY"] = "dummy"
s = m.fred("BAMLH0A0HYM2")
assert list(s.values) == [3.10], s
del os.environ["FRED_API_KEY"]
assert m.fred("DGS2").empty and m.fred("T10Y2Y").empty
assert sum("fredgraph" in c for c in calls) == 1, "CSV는 첫 실패 후 재시도하지 않음"
print("fred test OK")
