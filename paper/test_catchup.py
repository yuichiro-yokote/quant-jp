"""日次実行が「取りこぼしても追いつく」ことを、実データで確かめる。

    python paper/test_catchup.py

本番のジャーナルは触らない（環境変数で一時ディレクトリに逃がす）。
本番の `run_daily.py` には過去に遡る経路をわざと作っていないので、
遡って動かす必要があるこのテストだけが `process_day` を直接呼ぶ。

確かめること:
  1. 普通に毎日動く
  2. **3営業日止まっても、次の実行が3日ぶんまとめて処理して追いつく**
  3. 追いついた結果が、毎日動かした場合と**完全に一致する**
  4. 同じ日を2回処理しようとすると拒否される（冪等）
  5. ジャーナルを1文字書き換えると検出される（ハッシュチェーン）
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "backtest"))

N_DAYS = 40   # 直近40営業日で試す


def run_sequence(tmp: Path, days, panel, symbols, skip: set[int]) -> list[dict]:
    """skip に入っている番号の日は「実行されなかった」ことにして走らせる。"""
    os.environ["QUANTJP_JOURNAL_DIR"] = str(tmp)
    for m in ["journal", "run_daily"]:
        sys.modules.pop(m, None)
    import run_daily  # noqa: E402

    out = []
    pending: list = []
    for i, d in enumerate(days):
        pending.append(d)
        if i in skip:
            continue          # この日は実行されなかった → 未処理が溜まる
        for day in pending:   # 実行された日に、溜まったぶんをまとめて処理する
            out.append(run_daily.process_day(day, panel, symbols, dry=False))
        pending = []
    return out


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from dataset import load_panel, settled_dates
    import run_daily as rd

    symbols = rd.load_universe()
    print(f"候補 {len(symbols)} 銘柄の株価を取得中…")
    panel = load_panel(symbols)
    days = list(settled_dates(panel))[-N_DAYS:]
    print(f"検証期間: {days[0].date()} 〜 {days[-1].date()} ({len(days)} 営業日)")
    print()

    base = Path(tempfile.mkdtemp(prefix="quantjp_test_"))
    try:
        # --- 1. 毎日きちんと動いた場合 ---
        a_dir = base / "everyday"
        a = run_sequence(a_dir, days, panel, symbols, skip=set())
        print(f"1. 毎日実行          : {len(a)} 営業日を記録  "
              f"最終評価額 {a[-1]['equity']:,.0f}円")

        # --- 2. 途中で3営業日止まった場合 ---
        b_dir = base / "skipped"
        skip = {len(days) // 2, len(days) // 2 + 1, len(days) // 2 + 2}
        b = run_sequence(b_dir, days, panel, symbols, skip=skip)
        print(f"2. 3営業日止まった場合: {len(b)} 営業日を記録  "
              f"最終評価額 {b[-1]['equity']:,.0f}円")
        print(f"   （止めた日: {', '.join(str(days[i].date()) for i in sorted(skip))}）")

        # --- 3. 結果が一致するか ---
        assert len(a) == len(b), f"記録された営業日数が違う: {len(a)} vs {len(b)}"
        diffs = []
        for x, y in zip(a, b):
            if x["date"] != y["date"]:
                diffs.append(f"日付がずれた: {x['date']} vs {y['date']}")
            elif abs(x["equity"] - y["equity"]) > 0.01 or x["positions"] != y["positions"]:
                diffs.append(f"{x['date']}: 評価額 {x['equity']:,.2f} vs {y['equity']:,.2f}")
        if diffs:
            print("\n3. NG: 追いついた結果が毎日実行と一致しない")
            for d in diffs[:5]:
                print(f"     {d}")
            sys.exit(1)
        print("3. 一致              : OK  追いついた結果は毎日実行と完全に同じ")

        # --- 4. 冪等性 ---
        os.environ["QUANTJP_JOURNAL_DIR"] = str(a_dir)
        for m in ["journal", "run_daily"]:
            sys.modules.pop(m, None)
        import journal as J
        import run_daily as rd2
        try:
            rd2.process_day(days[-1], panel, symbols, dry=False)
            print("4. NG: 同じ日を2回処理できてしまった")
            sys.exit(1)
        except FileExistsError:
            print("4. 冪等性            : OK  同じ日の二重記録は拒否された")

        # --- 5. 改ざん検出 ---
        ok, _ = J.verify()
        assert ok, "改ざん前なのに検査が通らない"
        victim = J.path_for(a[len(a) // 2]["date"])
        e = json.loads(victim.read_text(encoding="utf-8"))
        e["cash"] = e["cash"] + 1        # 現金を1円だけ増やす
        victim.write_text(json.dumps(e, ensure_ascii=False, indent=2), encoding="utf-8")
        ok, problems = J.verify()
        if ok:
            print("5. NG: 改ざんを検出できなかった")
            sys.exit(1)
        print(f"5. 改ざん検出        : OK  1円の書き換えを検出（{len(problems)} 件の不整合）")
        print(f"     {problems[0]}")

        print()
        print("すべて通過。GitHub Actions が1日飛ばしても、翌日の実行が穴を埋める。")
    finally:
        shutil.rmtree(base, ignore_errors=True)
        os.environ.pop("QUANTJP_JOURNAL_DIR", None)


if __name__ == "__main__":
    main()
