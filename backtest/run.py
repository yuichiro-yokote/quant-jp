"""バックテスト実行。1回走らせるたびに試行ログへ1行追記される。

使い方:
    python backtest/run.py --symbol 7203.T --start 2015-01-01
    python backtest/run.py --symbol 7203.T --strategy buy_and_hold
    python backtest/run.py --trials          # これまでの試行を要約するだけ

前提条件は CRITERIA.md で固定済み（手数料・執行タイミング・初期資金・ベンチマーク）。
コマンドラインで変えられるようにはしてあるが、**変えたら結果は比較不能になる**。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backtesting import Backtest  # noqa: E402

import trials  # noqa: E402  (backtest/trials.py)
from dataset import buy_and_hold_return, load_bars  # noqa: E402
from strategies.sma_cross import BuyAndHold, SmaCross  # noqa: E402

# --- CRITERIA.md で固定した前提条件 ---
BENCHMARK = "1306.T"   # NEXT FUNDS TOPIX連動型上場投信
CASH = 3_000_000
COMMISSION = 0.0005    # 片道0.05%。現物手数料は無料の証券会社が多いがスリッページの代理
TRADE_ON_CLOSE = False  # False = 翌営業日の寄付で執行（当日終値でシグナル判定）

STRATEGIES = {
    "sma_cross": SmaCross,
    "buy_and_hold": BuyAndHold,
}


def run_one(args) -> None:
    strategy_cls = STRATEGIES[args.strategy]

    data = load_bars(args.symbol, args.start, args.end, refresh=args.refresh)
    actual_start = data.index[0].date().isoformat()
    actual_end = data.index[-1].date().isoformat()

    params = {}
    if args.strategy == "sma_cross":
        params = {"n_fast": args.fast, "n_slow": args.slow}
        if args.fast >= args.slow:
            sys.exit(f"短期({args.fast}) >= 長期({args.slow}) は無意味。値を見直すこと")

    bt = Backtest(
        data,
        strategy_cls,
        cash=CASH,
        commission=args.commission,
        trade_on_close=TRADE_ON_CLOSE,
        finalize_trades=True,  # 最終日に建玉が残っていても損益を確定して集計に含める
    )
    stats = bt.run(**params)

    # --- ベンチマーク（合格基準A1） ---
    # stats["Buy & Hold Return [%]"] は使わない。
    # backtesting.py は指標のウォームアップ期間だけ起点を後ろにずらすため、
    # 移動平均60日の戦略と単純保有とで **別の期間のB&Hが表示される**（実測: 148.62% vs 181.72%）。
    # 表示される Start/End は同じままなので気づきにくい。戦略間で比較するには自前で揃える。
    bh_topix = buy_and_hold_return(BENCHMARK, actual_start, actual_end, refresh=args.refresh)
    bh_symbol = buy_and_hold_return(args.symbol, actual_start, actual_end, refresh=args.refresh)
    ret = float(stats["Return [%]"])
    excess = ret - bh_topix

    # --- 合格基準A5: 最大利益トレードを1件除いても超過リターンが残るか ---
    # 複利を厳密には戻せないので概算。1発の大当たりへの依存を検出するのが目的で、
    # 小数点以下の精度は要らない。
    tr = stats["_trades"]
    best_pnl = float(tr["PnL"].max()) if len(tr) else 0.0
    ret_ex_best = (float(stats["Equity Final [$]"]) - best_pnl - CASH) / CASH * 100
    excess_ex_best = ret_ex_best - bh_topix

    metrics = {
        "n_trades": int(stats["# Trades"]),
        "return_pct": ret,
        "bh_symbol_pct": bh_symbol,
        "bh_topix_pct": bh_topix,
        "excess_vs_topix": excess,
        "excess_ex_best": excess_ex_best,
        "sharpe": float(stats.get("Sharpe Ratio", float("nan"))),
        "max_dd_pct": abs(float(stats["Max. Drawdown [%]"])),
        "win_rate_pct": float(stats.get("Win Rate [%]", float("nan"))),
        "profit_factor": float(stats.get("Profit Factor", float("nan"))),
    }

    trial_id = trials.record(
        strategy=args.strategy,
        params=params,
        symbol=args.symbol,
        start=actual_start,
        end=actual_end,
        commission=args.commission,
        metrics=metrics,
        note=args.note,
    )

    # --- 出力 ---
    print()
    print(f"===== 試行 #{trial_id} =====")
    print(f"戦略      : {args.strategy} {params}")
    print(f"銘柄      : {args.symbol}")
    print(f"期間      : {actual_start} 〜 {actual_end}  ({len(data)} 営業日)")
    print(f"手数料    : 片道 {args.commission * 100:.3f}%  / 執行: 翌営業日の寄付")
    print()
    print(f"戦略リターン          : {ret:8.2f} %")
    print(f"同銘柄バイ&ホールド   : {bh_symbol:8.2f} %  (差 {ret - bh_symbol:+.2f} %"
          f" ← 売買した意味があったか)")
    print(f"TOPIXバイ&ホールド    : {bh_topix:8.2f} %  ← 基準")
    print(f"対TOPIX超過リターン   : {excess:8.2f} %  [A1]")
    print(f"  最大利益トレード除く: {excess_ex_best:8.2f} %  [A5]")
    print()
    print(f"取引回数              : {metrics['n_trades']:8d} 回  [A3は年30回換算]")
    print(f"最大ドローダウン      : {metrics['max_dd_pct']:8.2f} %  [A2: 20%以内]")
    print(f"シャープレシオ        : {metrics['sharpe']:8.2f}")
    print(f"勝率                  : {metrics['win_rate_pct']:8.2f} %  (参考。損益比を見ること)")
    print(f"損益比(Profit Factor) : {metrics['profit_factor']:8.2f}")
    print()

    verdict = []
    verdict.append(("A1 対TOPIX超過 > 0", excess > 0))
    verdict.append(("A2 最大DD <= 20%", metrics["max_dd_pct"] <= 20))
    years = max((date.fromisoformat(actual_end) - date.fromisoformat(actual_start)).days / 365.25, 1e-9)
    verdict.append((f"A3 取引 >= 30回/年 (実績 {metrics['n_trades'] / years:.1f}回/年)",
                    metrics["n_trades"] / years >= 30))
    verdict.append(("A5 最大益トレード除いても超過 > 0", excess_ex_best > 0))
    for label, ok in verdict:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    print()
    print("--- 多重比較の確認 ---")
    print(trials.summary())
    print()

    # --- サバイバーシップバイアスの常時警告（CRITERIA.md セクションE）---
    # 「勝った」と「負けた」で証拠としての価値が逆になるので、文言を変える。
    # 黙って数字だけ出すと、人は必ず良い方の数字を信じる。
    print("=" * 70)
    print("【データの偏りについて】")
    print("  このバックテストは yfinance のデータで行っている。yfinanceは")
    print("  **現在も上場している銘柄しか返さない**（実測: 2年で6.7%が上場廃止し、")
    print("  それらは0行になる）。成績は実際より良く出るとは限らず、**歪む**。")
    print("  日本の上場廃止は倒産よりTOB・MBOが主で、消えた256銘柄の平均は +34.5%")
    print("  だった（生存は +28.5%）。買収された銘柄が見えないぶん、逆に過小評価もある。")
    print()
    if excess > 0:
        print("  >>> この試行は対TOPIXでプラスだが、**合格の根拠にはならない**。")
        print("      試行回数・過剰適合・データの歪みを、勝ちからは区別できない。")
        print("      判定は1年フォワードテストのみで行う（CRITERIA.md セクションE）。")
    else:
        print("  >>> この試行は対TOPIXで負けている。**これは証拠として採用してよい**。")
        print("      この条件で負けるなら、本番で勝てる理由がない。")
        print("      この戦略は捨ててよい。")
    print("=" * 70)

    if args.tearsheet:
        import tearsheet as ts

        r = ts.equity_to_returns(stats)
        b = ts.benchmark_returns(BENCHMARK, actual_start, actual_end)
        ts.explain(r, b)
        out = ts.REPORTS_DIR / f"trial{trial_id:03d}_{args.strategy}_{args.symbol.replace('.', '_')}.html"
        ts.build_html(r, b, out, title=f"試行#{trial_id} {args.strategy} {args.symbol}")
        print(f"\ntear sheet: {out}")

    if args.plot:
        out = REPO_ROOT / "reports" / f"trial{trial_id:03d}_{args.symbol.replace('.', '_')}.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        bt.plot(filename=str(out), open_browser=False)
        print(f"チャート: {out}")


def main() -> None:
    p = argparse.ArgumentParser(description="バックテストを1回実行し、試行ログに記録する")
    p.add_argument("--symbol", default="7203.T")
    # 2016-01-01 始まり: ベンチマーク1306.Tの2015-07-10に分割調整漏れの分配金があり、
    # それ以前の価格が約13%押し下げられている（dataset.py が検出してブロックする）。
    p.add_argument("--start", default="2016-01-01")
    p.add_argument("--end", default=date.today().isoformat())
    p.add_argument("--strategy", default="sma_cross", choices=sorted(STRATEGIES))
    p.add_argument("--fast", type=int, default=20)
    p.add_argument("--slow", type=int, default=60)
    p.add_argument("--commission", type=float, default=COMMISSION)
    p.add_argument("--note", default="")
    p.add_argument("--refresh", action="store_true", help="キャッシュを無視して取り直す")
    p.add_argument("--plot", action="store_true", help="チャートHTMLを出力")
    p.add_argument("--tearsheet", action="store_true",
                   help="QuantStatsのtear sheetを出力し、指標を日本語で解説")
    p.add_argument("--trials", action="store_true", help="試行ログの要約だけ表示して終了")
    args = p.parse_args()

    if args.trials:
        print(trials.summary())
        return

    run_one(args)


if __name__ == "__main__":
    main()
