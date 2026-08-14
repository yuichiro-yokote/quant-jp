"""複数銘柄ポートフォリオの執行シミュレータ（自作）。

なぜ自作か（2026-08-15、実測して決めた。README「ライブラリ選定」を参照）:
  既存ライブラリは軒並み**端株（小数株）を前提**にしている。日本株は100株単位でしか
  買えないため、この1点だけで同じ戦略の成績が **445% と 861%** に割れた。
  「どのライブラリが速いか」より、この前提を明示的に書けることの方が効く。
  vectorbt とは仮定を揃えれば 1.4% 以内で一致することを確認済み（照合用に残す）。

このモジュールが守る前提（CRITERIA.md で固定済み）:
  - 当日終値でシグナル判定 → **翌営業日の寄付で執行**
  - 手数料 片道 0.05%
  - **単元株 100株**。買えない銘柄は買えないまま（現金が遊ぶのを隠さない）
  - 信用取引なし。現金がマイナスになる注文は出さない

意図的にやらないこと:
  - 成行以外の注文、板・出来高の制約、ストップ高安。1年フォワードの検証には過剰。
  - 端株の許容。「実際には買えない売買」で成績を作らないため。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# 売買単位。**1 = 単元未満株（SBI証券のS株など）を使う前提**。
# 資金10万円では単元株(100株)だと36%の銘柄しか買えず、しかも小型・低流動性に偏るため
# 分散が成立しない。S株なら5銘柄でも遊休現金5.5%で回る（CRITERIA.md 前提条件を参照）。
# 単元株しか使えない口座を想定して検証したいときだけ lot=100 を渡す。
LOT = 1
COMMISSION = 0.0005  # 片道0.05%。S株は手数料無料だがスリッページの代理として残す


@dataclass
class Trade:
    """1銘柄の建玉が閉じたときの記録。合格基準A5（最大益トレードを除く）に使う。"""
    symbol: str
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    shares: float
    entry_price: float   # 手数料込みの平均取得単価
    exit_price: float    # 手数料差引後の平均売却単価
    pnl: float


@dataclass
class Result:
    equity: pd.Series               # 日次の資産評価額
    cash: pd.Series                 # 日次の現金
    trades: list[Trade]
    orders: int                     # 約定回数
    universe_log: pd.DataFrame      # 各リバランス日に「実際に何を持ったか」
    idle_cash_ratio: float          # 平均の遊休現金比率。単元株の壁の大きさが出る

    @property
    def return_pct(self) -> float:
        return (self.equity.iloc[-1] / self.equity.iloc[0] - 1) * 100

    @property
    def max_dd_pct(self) -> float:
        return abs(float((self.equity / self.equity.cummax() - 1).min()) * 100)

    @property
    def years(self) -> float:
        return (self.equity.index[-1] - self.equity.index[0]).days / 365.25

    def return_ex_best_trade_pct(self) -> float:
        """最大利益トレードを1件除いたリターン（A5の概算）。

        複利を厳密に戻すことはできない。1発の大当たりへの依存を検出するのが
        目的なので、最終資産から差し引く概算で足りる。
        """
        if not self.trades:
            return self.return_pct
        best = max(t.pnl for t in self.trades)
        start = self.equity.iloc[0]
        return (self.equity.iloc[-1] - best - start) / start * 100


class _Book:
    """保有株数と平均取得単価を持つだけの帳簿。売った時点で損益を確定する。"""

    def __init__(self, commission: float) -> None:
        self.commission = commission
        self.shares: dict[str, float] = {}
        self.cost: dict[str, float] = {}        # 手数料込みの平均取得単価
        self.entry: dict[str, pd.Timestamp] = {}
        self.trades: list[Trade] = []

    def buy(self, sym: str, qty: float, price: float, date: pd.Timestamp) -> float:
        paid = qty * price * (1 + self.commission)
        held = self.shares.get(sym, 0.0)
        if held == 0:
            self.entry[sym] = date
            self.cost[sym] = paid / qty
        else:
            self.cost[sym] = (self.cost[sym] * held + paid) / (held + qty)
        self.shares[sym] = held + qty
        return paid

    def sell(self, sym: str, qty: float, price: float, date: pd.Timestamp) -> float:
        got = qty * price * (1 - self.commission)
        unit = got / qty
        self.trades.append(Trade(
            symbol=sym, entry_date=self.entry[sym], exit_date=date, shares=qty,
            entry_price=self.cost[sym], exit_price=unit,
            pnl=(unit - self.cost[sym]) * qty,
        ))
        self.shares[sym] -= qty
        if self.shares[sym] <= 0:
            self.shares.pop(sym), self.cost.pop(sym), self.entry.pop(sym)
        return got

    def value(self, prices: pd.Series) -> float:
        return sum(q * prices.get(s, 0.0) for s, q in self.shares.items()
                   if pd.notna(prices.get(s)))


def run(
    close: pd.DataFrame,
    open_: pd.DataFrame,
    picks: pd.DataFrame,
    *,
    cash: float,
    lot: int = LOT,
    commission: float = COMMISSION,
) -> Result:
    """月末などのリバランス日の判定を受け取り、翌営業日の寄付で入れ替える。

    close, open_ : index=日付, columns=銘柄 の価格表
    picks        : 同じ形の bool 表。**True が立っている日 = リバランス日**。
                   その日の終値時点の判定なので、執行は翌営業日になる。
                   1行すべて False の日は「何もしない」。
    """
    dates = close.index
    book = _Book(commission)
    balance = float(cash)
    equity = pd.Series(index=dates, dtype=float)
    cash_series = pd.Series(index=dates, dtype=float)
    orders = 0
    idle: list[float] = []
    log: list[dict] = []

    # リバランス日 d の判定は、翌営業日 dates[i+1] に執行する
    rebal_days = picks.index[picks.any(axis=1)]
    pos = {d: i for i, d in enumerate(dates)}
    exec_of = {dates[pos[d] + 1]: d for d in rebal_days if pos[d] + 1 < len(dates)}

    for d in dates:
        if d in exec_of:
            signal_day = exec_of[d]
            px = open_.loc[d]
            wanted = [s for s in picks.columns[picks.loc[signal_day]]
                      if pd.notna(px.get(s)) and px.get(s, 0) > 0]

            # 1) 選から外れた銘柄を売る
            for s in list(book.shares):
                if s not in wanted and pd.notna(px.get(s)):
                    balance += book.sell(s, book.shares[s], float(px[s]), d)
                    orders += 1

            if wanted:
                # 2) 目標株数を決める。等ウェイト、単元株に切り捨て
                total = balance + sum(book.shares.get(s, 0.0) * float(px[s]) for s in wanted)
                budget = total / len(wanted)
                target = {}
                for s in wanted:
                    q = budget / (float(px[s]) * (1 + commission))
                    # lot=1 でも必ず切り捨てる。S株は1株単位であって小数株ではない。
                    target[s] = np.floor(q / lot) * lot

                # 3) **売りを先に全部処理してから買う。**
                #    買いと売りを交互に処理すると現金が尽きて買えない銘柄が出る。
                #    実測で、この順序だけでリターンが 445% → 861% 変わった。
                for s in wanted:
                    held = book.shares.get(s, 0.0)
                    if target[s] < held:
                        balance += book.sell(s, held - target[s], float(px[s]), d)
                        orders += 1
                for s in wanted:
                    held = book.shares.get(s, 0.0)
                    if target[s] <= held:
                        continue
                    # 現金が足りなければ「買える分だけ」買う。
                    # 「足りなければ見送る」にすると、必要額と残高がほぼ同額のときに
                    #   浮動小数の最下位ビットで買う/買わないが反転し、
                    #   同じ戦略のリターンが 160ポイント動いた（2026-08-15 実測）。
                    unit = float(px[s]) * (1 + commission)
                    qty = np.floor(min(target[s] - held, balance / unit) / lot) * lot
                    if qty > 0:
                        balance -= book.buy(s, qty, float(px[s]), d)
                        orders += 1

            now = balance + book.value(close.loc[d])
            if now > 0:
                idle.append(balance / now)
            log.append({
                "signal_date": signal_day, "exec_date": d,
                "wanted": " ".join(wanted),
                "held": " ".join(sorted(book.shares)),
                "cash": balance, "equity": now,
            })

        equity[d] = balance + book.value(close.loc[d])
        cash_series[d] = balance

    return Result(
        equity=equity, cash=cash_series, trades=book.trades, orders=orders,
        universe_log=pd.DataFrame(log),
        idle_cash_ratio=float(np.mean(idle)) if idle else 0.0,
    )
