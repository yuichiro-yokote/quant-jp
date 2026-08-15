"""週次レビュー — 週に1回、5分で状況を把握するためのもの。

    python paper/review.py

**この画面は「成績を見る」ためのものではない。** 合格基準（第2版）が問うのは
仕組みの健全性であって、勝ち負けではない（CRITERIA.md セクション0）。
成績を裸の数字で出すと必ず手を出したくなるので、**必ずノイズの幅と並べて出す**。
これは CRITERIA.md セクションD（開示義務）で決めたこと。

出すもの:
  1. 合格基準A1〜A5の現在地（1年で答えが出る問いだけ）
  2. 成績（足切り用。合格判定には使わない）とノイズの幅
  3. **A4の実測**: フォワードの記録を、バックテストのエンジンに通し直して突き合わせる
  4. 今週やること（たいてい「何もしない」）
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "backtest"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import journal  # noqa: E402
from dataset import buy_and_hold_return  # noqa: E402

BENCHMARK = "1306.T"
CASH = 100_000
HEARTBEAT_MAX = 5      # A1
TRADES_PER_YEAR = 30   # A3
HALT_DD = 30.0         # 中断条件D
TARGET_WEEKS = 52
MIN_DAYS_TO_JUDGE = 20  # これ未満の記録では成績を解釈しない（1ヶ月程度）

# research/power.py の実測値。保有10銘柄なら、実力ゼロでも1年で標準偏差14.2%ブレる。
NOISE_1Y = {1: 44.7, 5: 20.0, 10: 14.2, 20: 9.9, 50: 6.3}


def _git(*args: str) -> str:
    r = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=20)
    return r.stdout.strip()


def check_no_hand_edit() -> tuple[bool, str]:
    """A2: ジャーナルが「追加」以外の変更を受けていないか。

    ハッシュチェーンとは別の角度からの検査。記録ファイルは**追加されるだけ**が
    正しく、変更(M)や削除(D)が履歴に現れたら、それは手を入れた証拠になる。
    """
    modified = _git("log", "--diff-filter=MD", "--format=%h %ad %s", "--date=short",
                    "--", "paper/journal")
    if modified:
        return False, modified
    return True, ""


def noise_band(n_holdings: int, years: float) -> float:
    """経過期間に応じたノイズの幅（標準偏差, %）。時間の平方根で伸びる。"""
    sd = NOISE_1Y.get(n_holdings)
    if sd is None:
        keys = sorted(NOISE_1Y)
        sd = NOISE_1Y[min(keys, key=lambda k: abs(k - max(n_holdings, 1)))]
    return sd * (max(years, 1e-9) ** 0.5)


def replay_through_backtester(entries: list[dict]) -> tuple[float, float] | None:
    """A4: 記録された売買を、バックテスト側のエンジンで再現して突き合わせる。

    `run_daily.execute`（フォワード）と `portfolio.run`（バックテスト）は
    別々に書いた実装で、共有しているのは `allocate` だけ。だから両者を
    突き合わせれば、**片方だけにあるバグ**を見つけられる。
    ここが合わなくなったら、それ以降のバックテスト結果は一切信用できない。
    """
    import portfolio
    from dataset import load_panel

    dates = [pd.Timestamp(e["date"]) for e in entries]
    symbols = sorted({s for e in entries for s in e["signal"]["targets"]}
                     | {s for e in entries for s in e["positions"]})
    if not symbols or len(dates) < 2:
        return None

    start = (dates[0] - pd.Timedelta(days=10)).date().isoformat()
    panel = load_panel(symbols, start=start)
    close = panel["Close"].loc[dates[0]:dates[-1]]
    open_ = panel["Open"].loc[dates[0]:dates[-1]]
    if close.empty:
        return None

    # 記録に残っているシグナルを、そのまま picks 表に起こす
    picks = pd.DataFrame(False, index=close.index, columns=close.columns)
    for e in entries:
        if not e["signal"].get("rebalance"):
            continue
        d = pd.Timestamp(e["date"])
        if d not in picks.index:
            continue
        for s in e["signal"]["targets"]:
            if s in picks.columns:
                picks.loc[d, s] = True

    r = portfolio.run(close, open_, picks, cash=CASH, lot=1, commission=0.0005)
    return float(r.equity.iloc[-1]), float(entries[-1]["equity"])


def main() -> None:
    ds = journal.dates()
    if not ds:
        sys.exit("まだ記録がない。先に python paper/run_daily.py を実行すること。")
    entries = [journal.read(d) for d in ds]
    first, last = entries[0], entries[-1]

    start = pd.Timestamp(first["date"])
    end = pd.Timestamp(last["date"])
    # 経過期間は「記録した営業日数」でも測る。開始直後は暦日差が0になり、
    # ノイズの幅が0と出て「わずかな差」を有意だと誤判定するため。
    years = max((end - start).days / 365.25, len(ds) / 252, 1e-9)
    weeks = (end - start).days / 7

    print("=" * 68)
    print(f"週次レビュー  {pd.Timestamp.today().date()}")
    print(f"  {start.date()} 開始 / {len(ds)} 営業日を記録 / "
          f"{weeks:.0f}週目 (目標52週、残り {max(TARGET_WEEKS - weeks, 0):.0f}週)")
    print("=" * 68)

    # ---------- 合格基準 ----------
    print()
    print("【合格基準】1年で答えが出る問いだけ（CRITERIA.md セクションA）")

    chain_ok, chain_problems = journal.verify()
    edit_ok, edit_detail = check_no_hand_edit()
    n_fills = sum(len(e["fills"]) for e in entries)
    trades_per_year = n_fills / years
    eq = pd.Series([e["equity"] for e in entries],
                   index=[pd.Timestamp(e["date"]) for e in entries])
    max_dd = abs(float((eq / eq.cummax() - 1).min()) * 100)

    # A1: 記録の間隔。土日祝を挟むので、営業日ベースで見る
    gaps = [(pd.Timestamp(b) - pd.Timestamp(a)).days for a, b in zip(ds, ds[1:])]
    worst_gap = max(gaps) if gaps else 0

    rows = [
        ("A1 ハートビート欠測",
         worst_gap <= HEARTBEAT_MAX + 2,
         f"最大の間隔 {worst_gap} 日（上限 {HEARTBEAT_MAX} 営業日）"),
        ("A2 記録の手編集",
         chain_ok and edit_ok,
         "ハッシュチェーン健全 / 追加以外の変更なし" if (chain_ok and edit_ok)
         else "**問題あり。下に詳細**"),
        ("A3 取引回数",
         None if years < 0.25 else trades_per_year >= TRADES_PER_YEAR,
         f"{n_fills} 回（年換算 {trades_per_year:.0f} 回 / 必要 {TRADES_PER_YEAR} 回）"
         + ("　※3ヶ月未満は年換算が暴れるので判定しない" if years < 0.25 else "")),
        ("A5 中断条件",
         max_dd < HALT_DD,
         f"最大ドローダウン {max_dd:.1f}%（{HALT_DD:.0f}% で即停止）"),
    ]
    for label, ok, detail in rows:
        mark = "--" if ok is None else ("OK" if ok else "NG")
        print(f"  {mark:>2}  {label:<22} {detail}")

    if not chain_ok:
        for p in chain_problems:
            print(f"      {p}")
    if not edit_ok:
        print("      ジャーナルを変更/削除したコミット:")
        for line in edit_detail.splitlines():
            print(f"        {line}")

    # ---------- A4 ----------
    print()
    print("【A4 シミュレータと実記録の乖離】")
    try:
        res = replay_through_backtester(entries)
    except Exception as e:  # 取得失敗などで週次レビュー全体を止めない
        res = None
        print(f"  判定できず: {type(e).__name__}: {e}")
    if res is None:
        print("  まだ判定できない（売買が始まってから）")
    else:
        bt, fwd = res
        diff = (bt - fwd) / fwd * 100 if fwd else 0.0
        mark = "OK" if abs(diff) < 0.5 else "NG"
        print(f"  {mark}  バックテスト再現 {bt:,.0f}円 / 実記録 {fwd:,.0f}円  "
              f"乖離 {diff:+.2f}%")
        if abs(diff) >= 0.5:
            print("      **原因を特定するまで、以後のバックテスト結果は信用しないこと。**")

    # ---------- 成績（足切り用） ----------
    print()
    print("【成績】合格基準ではない。足切りにのみ使う（CRITERIA.md セクションB）")
    ret = (last["equity"] / CASH - 1) * 100
    try:
        bh = buy_and_hold_return(BENCHMARK, first["date"], last["date"])
    except Exception:
        bh = float("nan")
    excess = ret - bh
    band = noise_band(len(last["positions"]) or 10, years)
    print(f"  評価額     {last['equity']:>10,.0f} 円  ({ret:+.2f} %)")
    print(f"  TOPIX      {'':>10}     ({bh:+.2f} %)")
    print(f"  超過       {'':>10}     ({excess:+.2f} %)")
    print(f"  ノイズの幅 ±{band:.1f} %  ← 実力ゼロでもこの範囲は普通に動く")
    if len(ds) < MIN_DAYS_TO_JUDGE:
        print(f"  → **まだ何も言えない。** 記録が {len(ds)} 営業日しかない"
              f"（{MIN_DAYS_TO_JUDGE} 営業日を超えてから読むこと）")
    elif abs(excess) < band:
        print("  → **判断材料にならない。** ノイズの範囲内。ここで戦略を触らないこと")
    elif excess < -band:
        print("  → ノイズの幅を超えて負けている。足切りの検討対象（捨てる方向にのみ使う）")
    else:
        print("  → ノイズの幅を超えて勝っているが、**合格の根拠にはならない**")
        print("     実力ゼロでも約50%はプラスになる（CRITERIA.md セクション0）")

    # ---------- 保有 ----------
    print()
    print(f"【保有】{len(last['positions'])} 銘柄 / 現金 {last['cash']:,.0f} 円 "
          f"({last['cash'] / last['equity'] * 100:.1f} %)")
    for s, q in sorted(last["positions"].items()):
        print(f"    {s:>9}  {q:>6.0f} 株")

    # ---------- 今週やること ----------
    print()
    print("【今週やること】")
    todo = []
    if not (chain_ok and edit_ok):
        todo.append("**記録の整合性に問題がある。最優先で原因を特定する**")
    if res is not None and abs(res[0] - res[1]) / max(res[1], 1) * 100 >= 0.5:
        todo.append("**A4の乖離を説明する。ここが崩れると検証全体が無意味になる**")
    if max_dd >= HALT_DD:
        todo.append(f"**最大DDが{HALT_DD:.0f}%に達した。中断条件。停止して原因を記録する**")
    if worst_gap > HEARTBEAT_MAX + 2:
        todo.append("自動実行が止まっている。GitHub Actions のログを確認する")
    if not todo:
        print("  なし。**記録に手を出さないこと。**")
        print("  成績が悪くても戦略を変えない。変えたら1年を数え直しになる（鉄則2）。")
    else:
        for t in todo:
            print(f"  - {t}")
    print()


if __name__ == "__main__":
    main()
