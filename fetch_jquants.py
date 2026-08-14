"""J-Quants API (V2) の疎通確認。

事前に環境変数 JQUANTS_API_KEY を設定しておく。
  PowerShell: $env:JQUANTS_API_KEY = "取得したAPIキー"
"""

import os
import sys
from datetime import datetime, timedelta

import jquantsapi

if not os.environ.get("JQUANTS_API_KEY"):
    print("環境変数 JQUANTS_API_KEY が未設定です。")
    print('PowerShell で:  $env:JQUANTS_API_KEY = "取得したAPIキー"')
    sys.exit(1)

cli = jquantsapi.ClientV2()

# --- 上場銘柄一覧 ---
master = cli.get_eq_master()
print(f"上場銘柄一覧: {len(master)} 件")
print(f"カラム: {list(master.columns)}")
print()

# --- 直近1ヶ月の株価日足 ---
# 無料プランは直近2年分のみ取得できる点に注意
end = datetime.now()
start = end - timedelta(days=30)
bars = cli.get_eq_bars_daily_range(start_dt=start, end_dt=end)

print(f"株価日足: {len(bars)} 行")
print(f"期間: {bars['Date'].min()} 〜 {bars['Date'].max()}")
print(f"銘柄数: {bars['Code'].nunique()}")
print()
print(bars.head(3).to_string())
