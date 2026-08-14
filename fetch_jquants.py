"""J-Quants API (V2) の疎通確認。

.env に JQUANTS_API_KEY を書いておく（.env.example を参照）。

Freeプランの制約（実測で確認済み）:
  - データのカバー範囲は「約2年前 〜 約12週間前」。最新データは取得できない
  - レート制限が厳しく、全銘柄の並列取得はすぐ429になる
"""

import os
import sys
from datetime import datetime, timedelta

import jquantsapi
from dotenv import load_dotenv

load_dotenv()

if not os.environ.get("JQUANTS_API_KEY"):
    print(".env に JQUANTS_API_KEY が設定されていません。")
    print(".env.example をコピーして .env を作り、APIキーを記入してください。")
    sys.exit(1)

cli = jquantsapi.ClientV2()

# --- 上場銘柄一覧 ---
master = cli.get_eq_master()
print(f"上場銘柄一覧: {len(master)} 件")
print(master[["Code", "CoName", "MktNm", "S33Nm"]].head(3).to_string(index=False))
print()

# --- 株価日足（1銘柄だけ。全銘柄を並列で取ると429になる） ---
FREE_PLAN_DELAY_DAYS = 90  # 12週間 + 余裕
end = datetime.now() - timedelta(days=FREE_PLAN_DELAY_DAYS)
start = end - timedelta(days=30)

code = "7203"  # トヨタ自動車
bars = cli.get_eq_bars_daily(
    code=code,
    from_yyyymmdd=f"{start:%Y%m%d}",
    to_yyyymmdd=f"{end:%Y%m%d}",
)

print(f"{code} の日足: {len(bars)} 行")
print(f"期間: {bars['Date'].min()} 〜 {bars['Date'].max()}")
print(f"カラム: {list(bars.columns)}")
print()
print(bars.tail(5).to_string(index=False))
