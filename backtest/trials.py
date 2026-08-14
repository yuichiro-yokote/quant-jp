"""試行ログ — 「これは何回目の試行か」を消せない形で残す。

なぜ必要か:
  20通りの戦略を試して1つが good に見えたとき、それは戦略が良いのではなく
  **20回くじを引いただけ**かもしれない（多重比較の問題）。
  そして人間は、都合の悪い19回を自然に忘れる。忘れないために機械に数えさせる。

このログの価値は「消せないこと」にあるので:
  - 追記のみ。既存行の書き換え・削除はしない
  - Git管理下に置く（.gitignoreの対象にしない）
  - 失敗した試行・つまらない結果ほど残す。それが分母になる
"""

from __future__ import annotations

import csv
import json
import subprocess
from datetime import datetime
from pathlib import Path

TRIALS_CSV = Path(__file__).resolve().parent / "trials.csv"

FIELDS = [
    "trial_id",          # 通し番号。これが「何回目か」
    "timestamp",
    "git_commit",        # そのときのコード状態
    "strategy",
    "params",            # JSON文字列
    "symbol",
    "start",
    "end",
    "commission",
    "n_trades",
    "return_pct",        # 戦略のリターン
    "bh_symbol_pct",     # 同じ銘柄をバイ&ホールドした場合
    "bh_topix_pct",      # TOPIX(1306.T)をバイ&ホールドした場合
    "excess_vs_topix",   # 合格基準A1
    "excess_ex_best",    # 合格基準A5（最大利益トレードを除いた超過リターン）
    "sharpe",
    "max_dd_pct",
    "win_rate_pct",
    "profit_factor",
    "note",
]


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=TRIALS_CSV.parent.parent,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return out.stdout.strip() if out.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def next_trial_id() -> int:
    if not TRIALS_CSV.exists():
        return 1
    with TRIALS_CSV.open(encoding="utf-8", newline="") as f:
        return sum(1 for _ in csv.DictReader(f)) + 1


def record(
    *,
    strategy: str,
    params: dict,
    symbol: str,
    start: str,
    end: str,
    commission: float,
    metrics: dict,
    note: str = "",
) -> int:
    """1試行を追記し、その試行番号を返す。"""
    trial_id = next_trial_id()
    row = {
        "trial_id": trial_id,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "git_commit": _git_commit(),
        "strategy": strategy,
        "params": json.dumps(params, sort_keys=True),
        "symbol": symbol,
        "start": start,
        "end": end,
        "commission": commission,
        "note": note,
    }
    for k in FIELDS:
        if k not in row:
            v = metrics.get(k)
            row[k] = "" if v is None else (round(v, 4) if isinstance(v, float) else v)

    is_new = not TRIALS_CSV.exists()
    TRIALS_CSV.parent.mkdir(parents=True, exist_ok=True)
    with TRIALS_CSV.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if is_new:
            w.writeheader()
        w.writerow(row)

    return trial_id


def summary() -> str:
    """これまでの試行を要約する。数字を見る前に必ず読ませるための文言。"""
    if not TRIALS_CSV.exists():
        return "試行ログなし（これが1回目）"

    with TRIALS_CSV.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    n = len(rows)
    wins = [r for r in rows if r["excess_vs_topix"] not in ("", None)
            and float(r["excess_vs_topix"]) > 0]

    lines = [
        f"累計試行数: {n}",
        f"うち対TOPIX超過リターンがプラス: {len(wins)} 件",
    ]
    if n >= 5:
        rate = len(wins) / n * 100
        lines.append(
            f"→ {n}回試して{len(wins)}回勝ち ({rate:.0f}%)。"
            "「勝った戦略」を選ぶ前に、この分母を必ず併記すること"
        )
    return "\n".join(lines)
