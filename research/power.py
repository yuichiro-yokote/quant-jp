"""この検証は何年やれば答えが出るのか、を実データで測る（2026-08-15）。

CRITERIA.md セクション0 の根拠。合格基準を第2版に差し替える決め手になった。

    python research/power.py

やっていること:
  J-Quants の月次スナップショットから全銘柄の12ヶ月リターンを作り、
  **でたらめに N 銘柄選んで1年持つ**（＝実力ゼロの戦略）を何万回も試す。
  その超過リターンのばらつきが、「実力」を測るときのノイズの大きさになる。

結果（2026-08-15 実測）:
  5銘柄だと、実力ゼロでも1年の超過リターンは標準偏差 20.0%。
  現実的な実力は年3〜5%。**ノイズが実力の4〜7倍ある。**
  → 1年のフォワードテストでは「この戦略は勝てるか」を判定できない。
"""
from __future__ import annotations

import glob
import os
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPS = REPO_ROOT / "data" / "raw" / "jq_snapshots"
UNIVERSE = REPO_ROOT / "research" / "survivorship_result.csv"
HOLDINGS = [1, 5, 10, 20, 50]
TRIALS = 4000


def price_panel() -> pd.DataFrame:
    """月次スナップショットを (月 x 銘柄) の価格表にまとめる。"""
    codes = set(pd.read_csv(UNIVERSE)["code"].astype(str))
    cols = {}
    for f in sorted(glob.glob(str(SNAPS / "*.csv"))):
        d = pd.read_csv(f)
        d["Code"] = d["Code"].astype(str)
        d = d[d.Code.isin(codes) & d.AdjC.notna() & (d.AdjC > 0)]
        cols[os.path.basename(f)[:8]] = d.set_index("Code").AdjC
    panel = pd.DataFrame(cols).T
    panel.index = pd.to_datetime(panel.index, format="%Y%m%d")
    return panel


def yearly_returns(panel: pd.DataFrame) -> list[pd.Series]:
    """12ヶ月リターンを、重なりありの窓すべてで作る。"""
    out = []
    for i in range(len(panel) - 12):
        r = (panel.iloc[i + 12] / panel.iloc[i] - 1).dropna()
        # 単日クエリの AdjC は全期間で遡及調整されていないため、
        # 分割があると極端な値になる。中央付近の判断を歪めないよう外れ値を落とす。
        out.append(r[(r > -0.99) & (r < 5)])
    return out


def main() -> None:
    rng = np.random.default_rng(0)
    panel = price_panel()
    windows = yearly_returns(panel)
    print(f"価格パネル: {panel.shape[0]}ヶ月 x {panel.shape[1]:,}銘柄 / "
          f"12ヶ月リターンの窓 {len(windows)}本")
    print()
    print("【でたらめに N 銘柄選んで1年持つ = 実力ゼロの戦略】")
    print(f"{'保有銘柄数':>10} {'超過リターンのばらつき':>22} {'市場平均に勝つ確率':>20}")
    sd = {}
    for n in HOLDINGS:
        ex = []
        for r in windows:
            u = r.values
            m = u.mean()
            ex.extend(rng.choice(u, n, replace=False).mean() - m for _ in range(TRIALS))
        ex = np.array(ex)
        sd[n] = ex.std() * 100
        print(f"{n:>10} {sd[n]:>20.1f}% {(ex > 0).mean() * 100:>18.1f}%")
    print()
    print("  ※ 勝つ確率が50%を下回るのは、株のリターンが右に歪んでいるため。")
    print("     少数銘柄では『たいてい負けて、たまに大勝ち』になる。")
    print()

    print("【実力を「まぐれではない」と言えるまでに要する年数】(t値=2)")
    edges = [3, 5, 10, 20]
    print(f"{'保有銘柄数':>10} " + "".join(f"{f'実力{e}%/年':>13}" for e in edges))
    for n in HOLDINGS:
        row = f"{n:>10} "
        row += "".join(f"{(2 * sd[n] / e) ** 2:>11.0f}年" for e in edges)
        print(row)
    print()

    n = 5
    print(f"【1年だけ回して「対TOPIX超過 > 0」になる確率】{n}銘柄・ばらつき{sd[n]:.1f}%")
    for e in [0, 3, 5, 10, 20]:
        p = 1 - stats.norm.cdf(0, loc=e, scale=sd[n])
        print(f"  実力 {e:>2}%/年 → {p * 100:.0f}% で「合格」")
    print()
    print("  → 実力ゼロでも約半分は通る。1年の結果で合否を決めるのは、")
    print("     コインを1回投げてそのコインが歪んでいるか判定するのに近い。")


if __name__ == "__main__":
    main()
