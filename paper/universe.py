"""フォワードテストの候補銘柄リストを作る。

    python paper/universe.py            # paper/universe.csv を生成
    python paper/universe.py --show     # 中身を確認するだけ

なぜ絞るのか:
  投資対象は3,544銘柄あるが、日次で全部の株価を取りに行くと、取得だけで
  毎日数分かかり、失敗する確率もそのぶん上がる。**合格基準A1（欠測≤5営業日）を
  守ることの方が、候補を増やすことより優先される。**
  資金10万円・保有10銘柄なら、候補500もあれば選択肢としては十分に足りる。

絞り方（これがルール。変えたら `paper/universe.csv` を作り直して記録に残す）:
  1. プライム / スタンダード / グロース に上場（＝投資対象。TOKYO PRO MARKETは除く）
  2. 売買代金の中央値が上位 N 銘柄（既定 500）
  3. 1株の価格が予算を超える銘柄を除く（10万円を10銘柄なら1銘柄1万円）

注意:
  この絞り込みは「今の時点で流動性が高い銘柄」を選んでいるので、
  **過去に遡って使うとルックアヘッドになる**。フォワードテスト専用。
  バックテストにこのリストを使ってはいけない。
"""
from __future__ import annotations

import argparse
import glob
import os
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPS = REPO_ROOT / "data" / "raw" / "jq_snapshots"
INVESTABLE = REPO_ROOT / "research" / "survivorship_result.csv"
OUT = Path(__file__).resolve().parent / "universe.csv"

TOP_N = 500
CASH = 100_000
N_HOLDINGS = 10


def jq_to_yfinance(code: str) -> str:
    """J-Quantsの5桁コードを yfinance のティッカーに直す。

    JPXは4桁の証券コードの末尾に0を足して5桁にしている（7203 → 72030）。
    英数字コード（130A など）も同じ規則で5桁目が0になる。先頭4文字を取ればよい。
    """
    return f"{str(code)[:4]}.T"


def build(top_n: int = TOP_N) -> pd.DataFrame:
    files = sorted(glob.glob(str(SNAPS / "*.csv")))
    if not files:
        raise SystemExit(f"スナップショットが無い: {SNAPS}\n"
                         "先に research/survivorship.py を走らせること")

    inv = pd.read_csv(INVESTABLE)
    inv["code"] = inv["code"].astype(str)
    investable = set(inv["code"])

    # 売買代金と株価は、月ごとにぶれるので中央値を取る
    va, close = {}, {}
    for f in files:
        d = pd.read_csv(f)
        d["Code"] = d["Code"].astype(str)
        d = d[d.Code.isin(investable) & d.C.notna() & (d.C > 0)]
        key = os.path.basename(f)[:8]
        va[key] = d.set_index("Code").Va
        close[key] = d.set_index("Code").C

    va_med = pd.DataFrame(va).median(axis=1)
    px_med = pd.DataFrame(close).median(axis=1)

    df = pd.DataFrame({"turnover": va_med, "price": px_med}).dropna()
    df = df.join(inv.set_index("code")[["CoName", "MktNm"]])

    budget = CASH / N_HOLDINGS
    too_expensive = df.price > budget
    df = df[~too_expensive]

    df = df.nlargest(top_n, "turnover")
    df = df.reset_index(names="jq_code")
    df["symbol"] = df.jq_code.map(jq_to_yfinance)
    return df[["symbol", "jq_code", "CoName", "MktNm", "turnover", "price"]], int(too_expensive.sum())


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--top", type=int, default=TOP_N)
    p.add_argument("--show", action="store_true", help="生成せず、既存のリストを表示")
    args = p.parse_args()

    if args.show:
        if not OUT.exists():
            raise SystemExit(f"まだ無い: {OUT}")
        df = pd.read_csv(OUT)
        print(f"{OUT}  {len(df)} 銘柄")
        print(df.head(10).to_string(index=False))
        return

    df, n_expensive = build(args.top)
    df.to_csv(OUT, index=False, encoding="utf-8")

    budget = CASH / N_HOLDINGS
    print(f"候補銘柄: {len(df)} 銘柄 → {OUT}")
    print(f"  1株の価格が予算 {budget:,.0f}円 を超えて除外: {n_expensive} 銘柄")
    print(f"  売買代金の中央値: 最小 {df.turnover.min() / 1e6:,.0f}百万円/日 "
          f"/ 最大 {df.turnover.max() / 1e6:,.0f}百万円/日")
    print(f"  1株の価格: 中央値 {df.price.median():,.0f}円 / 最大 {df.price.max():,.0f}円")
    print()
    print("市場別:")
    print(df.MktNm.value_counts().to_string())
    print()
    print("注文額が1日の売買代金に占める割合（板への影響）:")
    r = (budget / df.turnover)
    print(f"  中央値 {r.median() * 100:.4f}%  最大 {r.max() * 100:.4f}%")


if __name__ == "__main__":
    main()
