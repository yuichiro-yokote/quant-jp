"""環境の疎通確認。J-Quants登録前でも動く yfinance でデータ取得まで試す。"""

import sys

import backtesting
import jquantsapi
import pandas as pd
import quantstats
import yfinance as yf

print(f"Python        : {sys.version.split()[0]}")
print(f"pandas        : {pd.__version__}")
print(f"yfinance      : {yf.__version__}")
print(f"backtesting   : {backtesting.__version__}")
print(f"quantstats    : {quantstats.__version__}")
print(f"jquants-api   : {jquantsapi.__version__}")
print("-" * 46)

# トヨタ自動車(7203)の日足。日本株は証券コード + ".T"
df = yf.Ticker("7203.T").history(period="1mo", interval="1d")

if df.empty:
    print("NG: データを取得できませんでした（ネットワークを確認）")
    sys.exit(1)

print(f"7203.T (トヨタ自動車) の日足を {len(df)} 件取得")
print(f"期間: {df.index[0]:%Y-%m-%d} 〜 {df.index[-1]:%Y-%m-%d}")
print()
print(df[["Open", "High", "Low", "Close", "Volume"]].tail(5).to_string())
