"""日次の記録層。**この検証で最も重要なファイル。**

合格基準A2は「`paper/journal/` の手編集 0件」。記録が信用できなければ、
1年かけた検証の価値はゼロになる。そのための作りが3つある:

  1. **1営業日1ファイル、追記のみ。** 既存ファイルは絶対に上書きしない
     （`write()` は既にあれば例外を投げる）。これで冪等性も同時に得られる。
  2. **ハッシュチェーン。** 各エントリは前日のエントリのハッシュを持つ。
     途中の1日を書き換えると、それ以降すべてのハッシュが合わなくなる。
     Git履歴を書き換えても検出できる。`verify()` で確認する。
  3. **状態を持たない。** 保有株数も現金も別ファイルに持たず、
     ジャーナルを最初から再生して求める（`replay()`）。
     「記録」と「状態」が食い違う余地を無くす。

エントリに入れるのは、**その日の時点で実際に分かっていたことだけ**:
  - その日の寄付で約定した内容（前日の終値シグナルによるもの）
  - 約定後の保有株数と現金
  - その日の終値で計算したシグナル（＝翌営業日の寄付で執行する目標）
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

# 本番の記録先。テストは環境変数で別の場所に逃がす（本物のジャーナルを汚さないため）。
JOURNAL_DIR = Path(os.environ.get("QUANTJP_JOURNAL_DIR")
                   or Path(__file__).resolve().parent / "journal")
GENESIS = "0" * 64


def _hash(entry: dict) -> str:
    """エントリのハッシュ。`hash` フィールド自身は除いて計算する。"""
    body = {k: v for k, v in entry.items() if k != "hash"}
    blob = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _git_commit() -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                           cwd=JOURNAL_DIR.parent.parent,
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def path_for(date: str) -> Path:
    return JOURNAL_DIR / f"{date}.json"


def dates() -> list[str]:
    """記録済みの営業日を古い順に返す。"""
    return sorted(p.stem for p in JOURNAL_DIR.glob("*.json"))


def read(date: str) -> dict:
    return json.loads(path_for(date).read_text(encoding="utf-8"))


def last() -> dict | None:
    d = dates()
    return read(d[-1]) if d else None


def write(date: str, *, fills: list[dict], positions: dict[str, float],
          cash: float, equity: float, signal: dict, universe: dict,
          notes: list[str] | None = None) -> dict:
    """1営業日分を記録する。**既にあれば例外。**上書きは仕様として許さない。"""
    p = path_for(date)
    if p.exists():
        raise FileExistsError(
            f"{p} は既にある。ジャーナルは追記のみ（合格基準A2）。\n"
            "書き直したい場合は、なぜ書き直すのかを別途記録に残したうえで、"
            "手で消すこと。**黙って上書きする経路はコードに用意しない。**"
        )
    prev = last()
    entry = {
        "date": date,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "git_commit": _git_commit(),
        "prev_hash": prev["hash"] if prev else GENESIS,
        "universe": universe,
        "fills": fills,
        "positions": {k: v for k, v in sorted(positions.items()) if v > 0},
        "cash": round(cash, 2),
        "equity": round(equity, 2),
        "signal": signal,
        "notes": notes or [],
    }
    entry["hash"] = _hash(entry)

    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return entry


def verify() -> tuple[bool, list[str]]:
    """ハッシュチェーンを検証する。壊れていれば、どこで壊れたかを返す。"""
    problems = []
    prev_hash = GENESIS
    for d in dates():
        e = read(d)
        if e.get("date") != d:
            problems.append(f"{d}: ファイル名と date フィールドが不一致 ({e.get('date')})")
        if e.get("prev_hash") != prev_hash:
            problems.append(
                f"{d}: prev_hash が前日と繋がらない。"
                f"期待 {prev_hash[:12]}… / 実際 {str(e.get('prev_hash'))[:12]}… "
                "→ **これより前の日が書き換えられている**"
            )
        recomputed = _hash(e)
        if e.get("hash") != recomputed:
            problems.append(f"{d}: この日の内容が hash と一致しない → **この日が書き換えられている**")
        prev_hash = e.get("hash", "")
    return (not problems), problems


def replay() -> dict:
    """ジャーナルを最初から再生して、いまの保有と現金を求める。

    別ファイルに状態を保存しない。記録が唯一の真実であるようにするため。
    """
    state = {"positions": {}, "cash": None, "last_date": None, "last_signal": None}
    for d in dates():
        e = read(d)
        state["positions"] = {k: float(v) for k, v in e["positions"].items()}
        state["cash"] = float(e["cash"])
        state["last_date"] = e["date"]
        state["last_signal"] = e.get("signal")
    return state
