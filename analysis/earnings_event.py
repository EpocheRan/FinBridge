"""历史财报披露日次日股价反应（600418）。"""
import io

import numpy as np
import pandas as pd
import requests

q = ("select period_end,publish_date,revenue,net_income_attributable_to_parent "
     "from fin_income_statement where code='600418' and period_end>='2021-01-01' order by period_end")
r = requests.get("http://127.0.0.1:9000/exp", params={"query": q}, timeout=60)
fin = pd.read_csv(io.StringIO(r.text), parse_dates=["period_end", "publish_date"])
fin["publish_date"] = fin["publish_date"].dt.tz_localize(None)
b = pd.read_csv(r"D:\FinBridge\analysis\data\bars_600418.csv", parse_dates=["ts"]).set_index("ts")

rows = []
for _, x in fin.iterrows():
    pub = x["publish_date"]
    if pd.isna(pub):
        continue
    idx = b.index[b.index >= pub]
    if len(idx) == 0:
        continue
    pos = b.index.get_loc(idx[0])
    if pos == 0:
        continue
    r1 = b["close"].iloc[pos] / b["close"].iloc[pos - 1] - 1
    r2 = (b["close"].iloc[pos + 1] / b["close"].iloc[pos - 1] - 1) if pos + 1 < len(b) else np.nan
    rows.append((x["period_end"].date(), pub.date(), round(r1 * 100, 2), round(r2 * 100, 2),
                 round(x["net_income_attributable_to_parent"] / 1e8, 2)))

print("报告期      披露日      次日%   两日累计%   单期归母(亿)")
for a, d, c, e, n in rows:
    print(f"{a}  {d}  {c:>7}  {e:>8}  {n:>9}")
absr = np.array([abs(r[2]) for r in rows])
print(f"\n样本 {len(rows)} 次：次日平均绝对涨跌 {absr.mean():.2f}%，中位数 {np.median(absr):.2f}%，最大 {absr.max():.2f}%")
print(f"次日上涨 {sum(1 for r in rows if r[2] > 0)} 次 / 下跌 {sum(1 for r in rows if r[2] < 0)} 次")
