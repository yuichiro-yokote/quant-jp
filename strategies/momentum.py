"""12-1モメンタム — 月末に、直近1ヶ月を除く12ヶ月リターンの上位N銘柄を持つ。

なぜこの戦略から始めるのか:
  **仕組みを検証するための当て馬であって、これで勝とうとはしていない。**
  合格基準（第2版）が問うのは「毎日確実に動くか」「シミュレータが現実を
  再現できているか」であって、戦略の優劣ではない（CRITERIA.md セクション0）。
  凝った戦略は、仕組みが1年動いてから差し替えればよい。

  そのうえで、当て馬としては妥当な選択:
  - 日本株でモメンタムが機能するという報告は多い
  - 直近1ヶ月を除くのは、短期反転（買われすぎた直後は下がりやすい）を避ける定石
  - 月次リバランスは、日次だと売買コストに負けるという調査結果に沿う
"""
from __future__ import annotations

import pandas as pd

LOOKBACK = 252   # 約12ヶ月
SKIP = 21        # 直近1ヶ月を除く
N_HOLDINGS = 10


def is_rebalance_day(dates: pd.DatetimeIndex, as_of: pd.Timestamp) -> bool:
    """as_of が「月が変わって最初の営業日」か。

    **月末ではなく月初で判定する。** 月末営業日かどうかは翌営業日が来るまで
    確定しない（急な休場がありうる）ため、フォワードテストでは判定できない。
    月初なら前営業日を見るだけで確定するので、未来を一切見ずに済む。

    バックテストとフォワードテストで**同じ関数を使うこと**。
    ここがずれると合格基準A4（シミュレータと実記録の乖離）が説明不能になる。

    （初期実装では「その月の最後の営業日」を、与えられたカレンダーの末尾で
      判定していた。フォワードでは as_of が常に末尾なので**毎日Trueになり**、
      月次のつもりが日次リバランスになっていた。2026-08-15 に修正。）
    """
    i = int(dates.searchsorted(as_of))
    if i <= 0 or i >= len(dates) or dates[i] != as_of:
        return False
    prev = dates[i - 1]
    return (prev.year, prev.month) != (as_of.year, as_of.month)


def score(close: pd.DataFrame, as_of: pd.Timestamp) -> pd.Series:
    """as_of の終値時点で計算できるモメンタム。**as_of より後の行は一切見ない。**"""
    hist = close.loc[:as_of]
    if len(hist) < LOOKBACK + SKIP + 1:
        return pd.Series(dtype=float)
    recent = hist.iloc[-(SKIP + 1)]
    past = hist.iloc[-(SKIP + LOOKBACK + 1)]
    return (recent / past - 1).dropna()


def select(close: pd.DataFrame, as_of: pd.Timestamp, *,
           n: int = N_HOLDINGS, max_price: float | None = None) -> list[str]:
    """as_of の終値時点で選ぶ上位n銘柄。翌営業日の寄付で執行する想定。

    max_price: 1株がこの価格を超える銘柄は選ばない。
        資金10万円・10銘柄なら1銘柄あたり1万円で、1株1.5万円の銘柄は
        **そもそも目標ウェイトで買えない**。それでも1株買うと、
        その1銘柄だけで資産の15%を占める集中投資になる（2026-08-15 に実際に発生）。
        買えない銘柄は候補から外し、次点を繰り上げる方が素直。
    """
    s = score(close, as_of)
    if max_price is not None:
        px = close.loc[:as_of].iloc[-1]
        affordable = px[px <= max_price].index
        s = s[s.index.isin(affordable)]
    if len(s) < n:
        return []
    return sorted(s.nlargest(n).index)
