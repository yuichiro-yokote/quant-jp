"""QuantStats で成績レポート(tear sheet)を出し、主要指標を日本語で解説する。

なぜ tear sheet が要るか:
  「リターン+109%」だけ見ても、それが**耐えられる**成績かは分からない。
  途中で40%減っていれば実弾では続けられないし、1銘柄の1回の当たりに
  支えられていれば再現しない。分布・ドローダウン・ベンチマークとの関係を
  一枚で見るためのもの。

注意: 同じ「シャープレシオ」でもライブラリで値が違う（実測で確認）
  7203.Tバイ&ホールドの同一データで backtesting.py は 0.338、QuantStats は 0.502。
  原因は分子の平均の取り方:
      QuantStats     : 算術平均 x252 = 14.09% → /28.07% = 0.502
      backtesting.py : 幾何平均(CAGR) = 10.69% → /28.07% = 0.381
  差 3.39ポイントは**ボラティリティ・ドラッグ**（≒σ²/2 = 3.94%）。
  値動きが荒いほど算術平均は実際の資産増加より大きく出る。

  → **数字の絶対値ではなく、同じ計算方法で比べた大小**を見ること。
     論文や商品説明のシャープを鵜呑みにできないのも同じ理由。
  ここでは QuantStats の既定（年252日、rf=0）に統一する。
  日本市場は年約245営業日だが、差は1.4%程度で推定誤差に埋もれる。
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pandas as pd
import quantstats as qs

REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"


def equity_to_returns(stats) -> pd.Series:
    """backtesting.py の結果から日次リターン系列を作る。"""
    eq = stats["_equity_curve"]["Equity"]
    r = eq.pct_change().dropna()
    r.index = pd.to_datetime(r.index)
    r.name = "Strategy"
    return r


def benchmark_returns(symbol: str, start: str, end: str) -> pd.Series:
    """ベンチマークの日次リターン。dataset側の検証を通った値だけを使う。"""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from dataset import load_bars

    b = load_bars(symbol, start, end)["Close"].pct_change().dropna()
    b.index = pd.to_datetime(b.index)
    b.name = "Benchmark"
    return b


def build_html(returns: pd.Series, bench: pd.Series, out: Path, title: str) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        qs.reports.html(returns, benchmark=bench, output=str(out), title=title)
    return out


def _safe(fn, *a, **kw):
    try:
        v = fn(*a, **kw)
        return float(v)
    except Exception:
        return float("nan")


def explain(returns: pd.Series, bench: pd.Series) -> None:
    """主要指標を、値つきで日本語解説する。ステップ4の本体はこちら。"""
    aligned = pd.concat([returns, bench], axis=1, join="inner").dropna()
    r, b = aligned.iloc[:, 0], aligned.iloc[:, 1]

    cagr = _safe(qs.stats.cagr, r) * 100
    cagr_b = _safe(qs.stats.cagr, b) * 100
    sharpe = _safe(qs.stats.sharpe, r)
    sharpe_b = _safe(qs.stats.sharpe, b)
    sortino = _safe(qs.stats.sortino, r)
    vol = _safe(qs.stats.volatility, r) * 100
    vol_b = _safe(qs.stats.volatility, b) * 100
    mdd = abs(_safe(qs.stats.max_drawdown, r)) * 100
    mdd_b = abs(_safe(qs.stats.max_drawdown, b)) * 100
    calmar = _safe(qs.stats.calmar, r)
    ulcer = _safe(qs.stats.ulcer_index, r)
    var = _safe(qs.stats.value_at_risk, r) * 100
    corr = float(r.corr(b))
    try:
        g = qs.stats.greeks(r, b)
        alpha, beta = float(g["alpha"]), float(g["beta"])
    except Exception:
        alpha = beta = float("nan")

    dd = qs.stats.to_drawdown_series(r)
    worst_dd_days = int((dd < 0).sum())
    exposure = float((r != 0).mean()) * 100

    rows = [
        ("CAGR（年平均成長率）", f"{cagr:7.2f} %", f"ベンチマーク {cagr_b:.2f} %",
         "累計リターンを年率に直したもの。期間の長さに依存しないので比較に使える"),
        ("年率ボラティリティ", f"{vol:7.2f} %", f"ベンチマーク {vol_b:.2f} %",
         "値動きの荒さ。**リターンが同じならこれが低い方が良い戦略**"),
        ("シャープレシオ", f"{sharpe:7.2f}", f"ベンチマーク {sharpe_b:.2f}",
         "リターン ÷ ボラティリティ。1.0で良好、2.0で優秀とされる。ただし1年程度では誤差が大きい"),
        ("ソルティノレシオ", f"{sortino:7.2f}", "",
         "下落方向の振れだけで割ったもの。**上に跳ねるのはリスクではない**という考え方"),
        ("最大ドローダウン", f"{mdd:7.2f} %", f"ベンチマーク {mdd_b:.2f} %",
         "高値からの最大下落幅。**合格基準A2はこれが20%以内**。実弾で耐えられるかを決める数字"),
        ("カルマーレシオ", f"{calmar:7.2f}", "",
         "CAGR ÷ 最大ドローダウン。「その痛みに見合うリターンか」"),
        ("アルサー指数", f"{ulcer:7.2f}", "",
         "含み損の深さと長さを合わせた指標。低いほど精神的に楽。**低い方が良い**"),
        ("日次VaR(95%)", f"{var:7.2f} %", "",
         "20日に1日はこれ以上の損が出る、という水準"),
        ("ベータ", f"{beta:7.2f}", "",
         "市場が1%動いたとき何%動くか。1.0なら市場と同じだけ振れている"),
        ("アルファ", f"{alpha:7.2f}", "",
         "市場の動きで説明できない超過分。**これが戦略の付加価値の候補**（ただし運と区別はつかない）"),
        ("対ベンチマーク相関", f"{corr:7.2f}", "",
         "**1.0に近いなら、それは戦略ではなく手数料の高いインデックス投資**。合格基準Bで見る項目"),
        ("含み損だった日数", f"{worst_dd_days:7d} 日", f"全 {len(r)} 日中",
         "「いつ勝つか」より「どれだけ耐えるか」。ここが長いと人間が先に折れる"),
        ("市場に居た割合", f"{exposure:7.1f} %", "",
         "現金でいた期間が長いほど、リターンが低くてもリスクは低い。単純比較の落とし穴"),
    ]

    print()
    print("=" * 78)
    print("指標の読み方（QuantStats既定: 年252日、リスクフリーレート0%）")
    print("=" * 78)
    for name, val, ref, desc in rows:
        print(f"\n{name}  {val}   {ref}")
        print(f"    {desc}")
    print()
    print("-" * 78)
    print("読む順番の目安:")
    print("  1. 最大ドローダウン → そもそも実弾で耐えられるか（A2）")
    print("  2. 対ベンチマーク相関 → インデックスの言い換えになっていないか")
    print("  3. CAGR をベンチマークと比較 → 売買した意味があったか（A1）")
    print("  4. シャープ/ソルティノ → 同じリターンをどれだけ穏やかに取れたか")
    print("  ※ 勝率は見ない。勝率9割でも1回の損で吹き飛ぶ戦略がある")
    print()
    print("※ ここのシャープは backtesting.py が上に出す値と一致しない（算術平均 vs 幾何平均）。")
    print("  ライブラリをまたいで数字を比べないこと。詳細はこのファイル冒頭のコメント。")
    print("-" * 78)
