"""yfinance の値が信用できるかを J-Quants(JPX公式) で検算する。

J-Quants Free は約12週間前までしか取れないが、その範囲は公式の正確な値。
yfinance を主データ源にする以上、両者が一致することを確認しておく必要がある。

コード体系の違いに注意:
  J-Quants: 72030 (4桁 + "0" の5桁)
  yfinance: 7203.T
"""

import os
import sys

import jquantsapi
import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

if not os.environ.get("JQUANTS_API_KEY"):
    print(".env に JQUANTS_API_KEY が設定されていません。")
    sys.exit(1)

CODE = "7203"  # トヨタ自動車
FROM, TO = "20260416", "20260515"

# --- J-Quants（公式・検算の基準） ---
cli = jquantsapi.ClientV2()
jq = cli.get_eq_bars_daily(code=CODE, from_yyyymmdd=FROM, to_yyyymmdd=TO)
jq = jq[["Date", "AdjC"]].copy()
jq["Date"] = pd.to_datetime(jq["Date"]).dt.date
jq = jq.rename(columns={"AdjC": "jquants"}).set_index("Date")

# --- yfinance（実運用で使う側） ---
yh = yf.Ticker(f"{CODE}.T").history(start="2026-04-16", end="2026-05-16")
yh = yh[["Close"]].copy()
yh.index = yh.index.date
yh = yh.rename(columns={"Close": "yfinance"})

# --- 突き合わせ ---
df = jq.join(yh, how="outer")
df["diff"] = (df["yfinance"] - df["jquants"]).abs()
df["diff_pct"] = df["diff"] / df["jquants"] * 100

print(df.to_string())
print()

missing = df[df.isna().any(axis=1)]
if len(missing):
    print(f"⚠ 片側にしか存在しない日: {len(missing)} 件")
    print(missing.to_string())
    print()

both = df.dropna()
tolerance = 0.5  # %
ng = both[both["diff_pct"] > tolerance]

print(f"照合日数      : {len(both)}")
print(f"最大乖離      : {both['diff_pct'].max():.4f} %")
print(f"許容({tolerance}%)超過: {len(ng)} 件")
print()
print("判定:", "OK — yfinance を主データ源にして問題ない" if len(ng) == 0 and len(missing) == 0
      else "NG — 乖離あり。原因を調べること")
