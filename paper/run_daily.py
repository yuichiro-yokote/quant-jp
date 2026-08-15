"""1年フォワードテストの日次実行。**取りこぼしても翌日に自動で追いつく。**

    python paper/run_daily.py              # 未処理の営業日をすべて処理する
    python paper/run_daily.py --dry-run    # 何をするか表示するだけ。記録しない
    python paper/run_daily.py --verify     # ジャーナルの改ざん検査だけ
    python paper/run_daily.py --status     # いまの保有と成績

なぜ「追いつく」作りなのか:
  合格基準A1は「ハートビート欠測 ≤ 5営業日」。しかしGitHub Actions の
  スケジュール実行は公式ドキュメントに「負荷が高いとキューのジョブが破棄される
  ことがある」と明記されている。**毎日必ず1回動く前提は置けない。**
  そこで「今日の分を処理する」ではなく「**まだ処理していない営業日を全部**処理する」
  設計にした。1日飛んでも、翌日の実行が自動で穴を埋める。

  同じ理由で、未確定のバーは処理しない。yfinance は当日のバーを出来高だけ入れて
  OHLC が NaN のまま先に配信することがある（実例: 7203.T 2026-08-14）。
  確定していない日は今日は飛ばし、翌日に処理する。

執行モデル（CRITERIA.md で凍結済み）:
  D日の終値でシグナル判定 → **D+1日の寄付で執行**
  したがって D日のエントリには「D日の寄付で約定した内容（D-1日のシグナル由来）」と
  「D日の終値で計算した、D+1日に執行する目標」の両方が入る。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "backtest"))

import journal  # noqa: E402  (paper/journal.py)
from dataset import load_panel, settled_dates  # noqa: E402
from portfolio import allocate  # noqa: E402
from strategies import momentum  # noqa: E402

UNIVERSE_CSV = Path(__file__).resolve().parent / "universe.csv"
CASH = 100_000        # CRITERIA.md 前提条件
COMMISSION = 0.0005   # 片道0.05%。S株は手数料無料だがスリッページの代理
LOT = 1               # 単元未満株（S株）を前提とする
MAX_CATCHUP = 30      # 一度に処理する営業日の上限。これを超えたら異常として止める


def load_universe() -> list[str]:
    if not UNIVERSE_CSV.exists():
        sys.exit(f"候補銘柄リストが無い: {UNIVERSE_CSV}\n先に python paper/universe.py を実行すること")
    return sorted(pd.read_csv(UNIVERSE_CSV)["symbol"].astype(str).tolist())


def execute(positions: dict[str, float], cash: float, targets: list[str],
            open_px: pd.Series) -> tuple[dict[str, float], float, list[dict]]:
    """寄付で目標の保有に組み替える。バックテストと**同じ allocate** を使う。

    ここがバックテストとずれると、合格基準A4（シミュレータと実記録の乖離）が
    説明不能になる。売買のロジックを2箇所に書かないこと。
    """
    fills: list[dict] = []
    pos = dict(positions)

    tradable = [s for s in targets if pd.notna(open_px.get(s)) and open_px.get(s, 0) > 0]

    # 1) 選から外れた銘柄を売る
    for s in sorted(pos):
        if s in tradable or pos[s] <= 0:
            continue
        px = open_px.get(s)
        if pd.isna(px) or px <= 0:
            continue  # 値が付かない日は売れない。翌日に持ち越す
        cash += pos[s] * float(px) * (1 - COMMISSION)
        fills.append({"symbol": s, "side": "sell", "shares": pos[s], "price": float(px)})
        pos[s] = 0.0

    if tradable:
        total = cash + sum(pos.get(s, 0.0) * float(open_px[s]) for s in tradable)
        target = allocate({s: float(open_px[s]) for s in tradable},
                          total, lot=LOT, commission=COMMISSION)

        # 2) 売りを先に全部処理してから買う（現金を作ってから使う）
        for s in sorted(tradable):
            held = pos.get(s, 0.0)
            if target[s] < held:
                qty = held - target[s]
                cash += qty * float(open_px[s]) * (1 - COMMISSION)
                fills.append({"symbol": s, "side": "sell", "shares": qty, "price": float(open_px[s])})
                pos[s] = target[s]
        for s in sorted(tradable):
            held = pos.get(s, 0.0)
            if target[s] <= held:
                continue
            unit = float(open_px[s]) * (1 + COMMISSION)
            qty = int(min(target[s] - held, cash / unit) // LOT * LOT)
            if qty > 0:
                cash -= qty * unit
                fills.append({"symbol": s, "side": "buy", "shares": qty, "price": float(open_px[s])})
                pos[s] = held + qty

    return {k: v for k, v in pos.items() if v > 0}, cash, fills


def process_day(day: pd.Timestamp, panel: dict, symbols: list[str], *, dry: bool) -> dict:
    state = journal.replay()
    prev = journal.last()
    date_s = day.date().isoformat()

    positions = state["positions"]
    cash = state["cash"] if state["cash"] is not None else float(CASH)

    # 1) 前日の終値シグナルを、この日の寄付で執行する
    #    **前日がリバランス日でなければ、何も売買しない。**
    #    ここを「目標＝現在の保有」として execute に渡すと、allocate が毎日
    #    等ウェイトへ戻そうとして日々売買が発生する（月次のつもりが日次になる）。
    fills: list[dict] = []
    prev_signal = prev.get("signal", {}) if prev else {}
    if prev_signal.get("rebalance"):
        positions, cash, fills = execute(
            positions, cash, prev_signal.get("targets", []), panel["Open"].loc[day])

    # 2) この日の終値でシグナルを計算する（執行は翌営業日）
    close = panel["Close"]
    dates_upto = close.index[close.index <= day]
    rebalance = momentum.is_rebalance_day(dates_upto, day)
    if rebalance:
        # いま買える価格帯の銘柄だけを候補にする。
        # 1株が1銘柄あたり予算を超える銘柄は、目標ウェイトでは持てない。
        equity_now = cash + sum(q * float(close.loc[day].get(s, 0) or 0)
                                for s, q in positions.items())
        targets = momentum.select(close.loc[:day], day,
                                  max_price=equity_now / momentum.N_HOLDINGS)
    else:
        targets = sorted(positions)

    # 3) この日の終値で評価する
    close_row = close.loc[day]
    holdings_value = sum(q * float(close_row[s]) for s, q in positions.items()
                         if s in close_row.index and pd.notna(close_row[s]))
    equity = cash + holdings_value

    notes = []
    missing = [s for s in positions if s not in close_row.index or pd.isna(close_row.get(s))]
    if missing:
        notes.append(f"終値が取れなかった保有銘柄: {' '.join(sorted(missing))}（評価額に含めていない）")

    entry = {
        "date": date_s, "fills": fills, "positions": positions, "cash": cash,
        "equity": equity,
        "signal": {"as_of_close": date_s, "rebalance": bool(rebalance),
                   "strategy": "momentum_12_1", "targets": targets},
        "universe": {"n": len(symbols), "file": UNIVERSE_CSV.name,
                     "n_priced": int(close_row.notna().sum())},
        "notes": notes,
    }
    if not dry:
        journal.write(date_s, fills=fills, positions=positions, cash=cash, equity=equity,
                      signal=entry["signal"], universe=entry["universe"], notes=notes)
    return entry


def show_status() -> None:
    st = journal.replay()
    ds = journal.dates()
    if not ds:
        print("まだ記録なし。")
        return
    first, last = journal.read(ds[0]), journal.read(ds[-1])
    ok, problems = journal.verify()
    print(f"記録期間  : {first['date']} 〜 {last['date']}  ({len(ds)} 営業日)")
    print(f"評価額    : {last['equity']:,.0f} 円  (開始 {CASH:,} 円 / "
          f"{(last['equity'] / CASH - 1) * 100:+.2f} %)")
    print(f"現金      : {last['cash']:,.0f} 円  ({last['cash'] / last['equity'] * 100:.1f} %)")
    print(f"保有      : {len(last['positions'])} 銘柄")
    for s, q in sorted(last["positions"].items()):
        print(f"    {s}: {q:.0f} 株")
    n_fills = sum(len(journal.read(d)["fills"]) for d in ds)
    years = max((pd.Timestamp(last["date"]) - pd.Timestamp(first["date"])).days / 365.25, 1e-9)
    print(f"約定      : {n_fills} 回  ({n_fills / years:.1f} 回/年)  [A3: 30回/年以上]")
    print(f"改ざん検査: {'OK（チェーン健全）' if ok else 'NG'}")
    for p in problems:
        print(f"    {p}")


def main() -> None:
    ap = argparse.ArgumentParser(description="フォワードテストの日次実行（取りこぼしに追いつく）")
    ap.add_argument("--dry-run", action="store_true", help="記録せず、何をするかだけ表示")
    ap.add_argument("--verify", action="store_true", help="ジャーナルの改ざん検査だけ")
    ap.add_argument("--status", action="store_true", help="いまの保有と成績")
    ap.add_argument("--refresh", action="store_true", help="価格キャッシュを無視して取り直す")
    args = ap.parse_args()

    if args.verify:
        ok, problems = journal.verify()
        print("OK: ジャーナルは改ざんされていない" if ok else "NG: ジャーナルに問題がある")
        for p in problems:
            print(f"  {p}")
        sys.exit(0 if ok else 1)

    if args.status:
        show_status()
        return

    ok, problems = journal.verify()
    if not ok:
        print("ジャーナルの検査に失敗した。追記する前に原因を特定すること:")
        for p in problems:
            print(f"  {p}")
        sys.exit(1)

    symbols = load_universe()
    panel = load_panel(symbols, refresh=args.refresh)
    settled = settled_dates(panel)
    done = set(journal.dates())

    started = journal.dates()
    if started:
        # 記録済みの最終日より後の、確定した営業日を処理する
        todo = [d for d in settled if d.date().isoformat() not in done
                and d > pd.Timestamp(started[-1])]
    else:
        # **初回は今日から始める。過去には遡らない**（遡ったらそれはバックテスト）
        todo = [settled[-1]]

    if not todo:
        print(f"処理する営業日なし。最新の確定営業日 {settled[-1].date()} は記録済み。")
        return

    if len(todo) > MAX_CATCHUP:
        print(f"未処理が {len(todo)} 営業日ある（上限 {MAX_CATCHUP}）。")
        print("長期間止まっていた可能性が高い。原因を確認してから手動で処理すること。")
        sys.exit(1)

    if len(todo) > 1:
        print(f"** 取りこぼしを検出: {len(todo)} 営業日ぶんをまとめて処理する **")

    for day in todo:
        e = process_day(day, panel, symbols, dry=args.dry_run)
        head = "[dry-run] " if args.dry_run else ""
        sig = e["signal"]
        print(f"{head}{e['date']}  評価額 {e['equity']:>10,.0f}円  現金 {e['cash']:>8,.0f}円  "
              f"保有 {len(e['positions']):>2}銘柄  約定 {len(e['fills']):>2}件"
              f"{'  ← リバランス日' if sig['rebalance'] else ''}")
        for n in e["notes"]:
            print(f"    注意: {n}")


if __name__ == "__main__":
    main()
