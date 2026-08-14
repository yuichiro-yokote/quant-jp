"""複数銘柄バックテストのライブラリ選定に使った実測（2026-08-15）。

README「複数銘柄バックテストのライブラリ選定」に載せた表を、この1本で再現する。
結論を後から検算できるように残してある。**本番の戦略ではない。**

    python research/lib_shootout.py            # 自作エンジンで前提を1つずつ変えて測る
    python research/lib_shootout.py --vbt      # vectorbt と突き合わせる（別環境が要る）

vectorbt は本番の依存に入れない。照合したいときだけ:
    python -m venv .venv-vbt && .venv-vbt/Scripts/pip install vectorbt
    .venv-vbt/Scripts/python research/lib_shootout.py --vbt

検証した戦略:
    12-1モメンタム（直近1ヶ月を除く12ヶ月リターン）
    毎月末に上位5銘柄を等ウェイト / ロングのみ
    月末終値でシグナル判定 → 翌営業日の寄付で執行 / 片道0.05%
"""
from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "backtest"))

CACHE = REPO_ROOT / "data" / "raw" / "shootout_universe.csv"
START = "2016-01-01"
CASH = 3_000_000
COMMISSION = 0.0005
TOP_N = 5
LOOKBACK = 252   # 約12ヶ月
SKIP = 21        # 直近1ヶ月を除く

# 東証プライムの流動性が高い銘柄を業種を散らして40。評価用なので選び方は恣意的でよい。
SYMBOLS = [
    "7203.T", "6758.T", "9432.T", "8306.T", "6861.T", "9984.T", "6098.T", "4063.T",
    "8035.T", "7974.T", "9433.T", "4519.T", "6501.T", "8058.T", "8001.T", "8031.T",
    "6902.T", "7267.T", "4568.T", "6367.T", "6981.T", "4661.T", "8766.T", "8316.T",
    "9022.T", "9020.T", "2914.T", "4502.T", "6503.T", "7741.T", "6954.T", "6273.T",
    "4901.T", "3382.T", "8411.T", "8591.T", "5108.T", "7011.T", "6146.T", "9101.T",
]


def load() -> tuple[pd.DataFrame, pd.DataFrame]:
    if not CACHE.exists():
        import yfinance as yf
        raw = yf.download(SYMBOLS, start="2015-01-01", auto_adjust=True,
                          progress=False, group_by="column", threads=True)
        parts = [raw[f].stack().rename(f) for f in ["Open", "High", "Low", "Close", "Volume"]]
        df = pd.concat(parts, axis=1).reset_index()
        df.columns = ["date", "symbol", "open", "high", "low", "close", "volume"]
        df.dropna(subset=["close"]).sort_values(["date", "symbol"]).to_csv(CACHE, index=False)
        print(f"取得して保存: {CACHE}")

    df = pd.read_csv(CACHE, parse_dates=["date"])
    close = df.pivot(index="date", columns="symbol", values="close").sort_index()
    open_ = df.pivot(index="date", columns="symbol", values="open").sort_index()
    return close, open_


def signals(close: pd.DataFrame) -> pd.DataFrame:
    """各月末に「上位5銘柄か」を True/False で立てた表を返す。月末以外は全 False。"""
    mom = close.shift(SKIP) / close.shift(SKIP + LOOKBACK) - 1
    month_ends = close.groupby(close.index.to_period("M")).tail(1).index
    month_ends = month_ends[month_ends >= START]

    picks = pd.DataFrame(False, index=close.index, columns=close.columns)
    for d in month_ends:
        row = mom.loc[d].dropna()
        if len(row) >= TOP_N:
            picks.loc[d, row.nlargest(TOP_N).index] = True
    return picks


def _sweep(close, open_, picks, *, lot: int, sell_first: bool):
    """前提を切り替えられる簡易版。portfolio.py は sell_first=True 固定の実装。"""
    dates = close.index
    cash = float(CASH)
    shares = pd.Series(0.0, index=close.columns)
    equity = pd.Series(index=dates, dtype=float)
    n, idle = 0, []

    pos = {d: i for i, d in enumerate(dates)}
    rebal = picks.index[picks.any(axis=1)]
    exec_of = {dates[pos[d] + 1]: d for d in rebal if pos[d] + 1 < len(dates)}

    for d in dates:
        if d in exec_of:
            px = open_.loc[d]
            want_syms = [s for s in picks.columns[picks.loc[exec_of[d]]]
                         if pd.notna(px.get(s)) and px.get(s, 0) > 0]
            drop = list(shares[shares > 0].index) if not sell_first \
                else [s for s in shares[shares > 0].index if s not in want_syms]
            for s in drop:
                if pd.notna(px[s]):
                    cash += shares[s] * px[s] * (1 - COMMISSION)
                    shares[s] = 0.0
                    n += 1
            if want_syms:
                total = cash + sum(shares[s] * px[s] for s in want_syms)
                budget = total / len(want_syms)
                tgt = {}
                for s in want_syms:
                    q = budget / (px[s] * (1 + COMMISSION))
                    tgt[s] = np.floor(q / lot) * lot if lot > 1 else q
                for s in want_syms:                      # 売りを先に
                    if tgt[s] < shares[s]:
                        cash += (shares[s] - tgt[s]) * px[s] * (1 - COMMISSION)
                        shares[s] = tgt[s]
                        n += 1
                for s in want_syms:                      # そのあと買い
                    if tgt[s] <= shares[s]:
                        continue
                    unit = px[s] * (1 + COMMISSION)
                    q = min(tgt[s] - shares[s], cash / unit)  # 買える分だけ買う
                    if lot > 1:
                        q = np.floor(q / lot) * lot
                    if q > 0:
                        cash -= q * unit
                        shares[s] += q
                        n += 1
            now = cash + (shares * close.loc[d].fillna(0)).sum()
            if now > 0:
                idle.append(cash / now)
        equity[d] = cash + (shares * close.loc[d].fillna(0)).sum()

    return equity.loc[START:], n, float(np.mean(idle)) if idle else 0.0


def run_vbt(close, open_, picks, *, granularity):
    import vectorbt as vbt
    w = picks.astype(float)
    w = w.div(w.sum(axis=1).replace(0, np.nan), axis=0)
    w[~picks.any(axis=1).values] = np.nan
    w = w.shift(1)  # 翌営業日に執行
    pf = vbt.Portfolio.from_orders(
        close=open_, size=w, size_type="targetpercent", init_cash=CASH,
        fees=COMMISSION, cash_sharing=True, group_by=True, call_seq="auto",
        size_granularity=granularity, freq="1D")
    eq = pf.value().loc[START:]
    return eq, len(pf.orders.records_readable)


def show(label: str, eq: pd.Series, n: int, idle: float | None = None) -> None:
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    ret = (eq.iloc[-1] / eq.iloc[0] - 1) * 100
    dd = (eq / eq.cummax() - 1).min() * 100
    tail = f"{idle * 100:>8.1f}%" if idle is not None else f"{'—':>9}"
    print(f"{label:>28} {ret:>10.2f}% {dd:>9.2f}% {n / years:>9.1f} {tail}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--vbt", action="store_true", help="vectorbt と突き合わせる")
    args = p.parse_args()

    close, open_ = load()
    picks = signals(close)

    print(f"{'':>28} {'リターン':>11} {'最大DD':>9} {'約定/年':>9} {'遊休現金':>9}")
    print("-" * 72)
    for lot, sell_first in itertools.product([100, 1], [False, True]):
        eq, n, idle = _sweep(close, open_, picks, lot=lot, sell_first=sell_first)
        way = "差分のみ" if sell_first else "全部売買"
        show(f"自作 単元{lot}株 / {way}", eq, n, idle)

    if args.vbt:
        print()
        for gran in [None, 100]:
            eq, n = run_vbt(close, open_, picks, granularity=gran)
            show(f"vectorbt granularity={gran}", eq, n)
        print()
        print("※ 自作『単元1株/差分のみ』と vectorbt『granularity=None』が一致すれば、")
        print("   両エンジンとも正しい。2026-08-15の実測では 861.55% vs 849.49%、")
        print("   約定回数はどちらも 75.4回/年で一致した。")

    # 単元株の壁
    print()
    last = close.iloc[-1].dropna()
    budget = CASH / TOP_N
    over = last[last * 100 > budget]
    print(f"直近終値で「100株が予算 {budget:,.0f}円 を超える」銘柄: {len(over)} / {len(last)}")
    for s, px in over.sort_values(ascending=False).head(5).items():
        print(f"    {s}: {px:,.0f}円 → 100株 = {px * 100:,.0f}円")


if __name__ == "__main__":
    main()
