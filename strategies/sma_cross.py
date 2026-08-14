"""移動平均クロス — 教科書的な戦略の代表。

最初にこれを回すのは、勝つためではなく **「素朴な戦略はほとんど勝てない」を
自分の目で確認するため**。ここを飛ばして凝った戦略から始めると、
「勝てないのが普通」という基準線を持たないまま数字を見ることになり、
たまたま良かった結果を実力だと誤認する。

現物ロングのみ（空売りしない）。信用取引は前提が変わるため対象外にする。
"""

import pandas as pd
from backtesting import Strategy
from backtesting.lib import crossover


def SMA(values, n: int) -> pd.Series:
    return pd.Series(values).rolling(n).mean()


class SmaCross(Strategy):
    """短期線が長期線を上抜けたら買い、下抜けたら手仕舞い。"""

    n_fast = 20
    n_slow = 60

    def init(self):
        close = self.data.Close
        self.sma_fast = self.I(SMA, close, self.n_fast)
        self.sma_slow = self.I(SMA, close, self.n_slow)

    def next(self):
        if crossover(self.sma_fast, self.sma_slow):
            self.buy()
        elif crossover(self.sma_slow, self.sma_fast):
            self.position.close()


class BuyAndHold(Strategy):
    """初日に買って何もしない。比較のための下限ライン。

    多くの戦略はこれに勝てない。勝てないなら、その戦略は
    「手数料と手間をかけて成績を下げる装置」でしかない。
    """

    def init(self):
        pass

    def next(self):
        if not self.position:
            self.buy()
