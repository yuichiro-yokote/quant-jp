"""サバイバーシップバイアスの大きさを実測する。

問題:
  現在上場している銘柄だけでスクリーニングすると、途中で上場廃止になった銘柄が
  **最初から存在しなかったこと**になる。倒産で価値が消えた銘柄の損失がデータから
  抜け落ちるので、バックテストのリターンは過大に、ドローダウンは過小に出る。

取得方針（レート制限対策）:
  1銘柄ずつ引くと J-Quants Free では即座に締め出される（実測: 2件目以降が全滅）。
  代わりに **「ある日の全銘柄」を1リクエストで取る** get_eq_bars_daily(date=...) を使い、
  月1回のスナップショットを24回だけ取る。これで全銘柄をカバーできる。

  各スナップショットは data/raw/ にキャッシュするので、途中で失敗しても取り直しは不要。
  消えた銘柄の「最後の価格」は、その銘柄が最後に現れたスナップショットの終値で近似する
  （最大1ヶ月のずれが出るが、バイアスの大きさを測る目的には十分）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import jquantsapi
import pandas as pd
from dotenv import load_dotenv

load_dotenv()
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

CACHE = REPO / "data" / "raw" / "jq_snapshots"
OUT = Path(__file__).resolve().parent / "survivorship_result.csv"

# Freeプランのカバー範囲内（実測: 2024-05-22 〜 2026-05-22）
MONTHS = pd.date_range("2024-06-01", "2026-05-01", freq="MS")

# J-Quants Free は「秒あたり」ではなく**クォータ**で制限しているらしい。
# 実測: 10〜15リクエストで締め出され、その後数分〜十数分は何を投げても弾かれる。
# 土日祝かどうかに関係なく失敗するので、失敗＝レート制限とみなして気長に待つ。
SLEEP = 45
MAX_RETRY = 6
BACKOFF = 90        # 失敗時の待ち（回を追うごとに伸ばす）

INVESTABLE_MKT = ["プライム", "スタンダード", "グロース"]


def fetch_snapshot(cli, day: pd.Timestamp) -> pd.DataFrame | None:
    """その日の全銘柄の日足を1リクエストで取る。キャッシュがあれば使う。"""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{day:%Y%m%d}.csv"
    if path.exists():
        return pd.read_csv(path, dtype={"Code": str})

    for attempt in range(MAX_RETRY):
        try:
            # 引数名は date ではなく date_yyyymmdd（date= を渡すと TypeError になる）
            df = cli.get_eq_bars_daily(date_yyyymmdd=f"{day:%Y%m%d}")
            if df is not None and len(df):
                df.to_csv(path, index=False)
                return df
            return None  # 休場日（データはあるが0行）
        except Exception as e:
            wait = BACKOFF * (attempt + 1)
            print(f"    {day:%Y-%m-%d} 失敗({type(e).__name__}) {wait}秒待つ", flush=True)
            time.sleep(wait)
    return None


def main() -> None:
    cli = jquantsapi.ClientV2()

    # --- 各月の営業日スナップショットを集める ---
    snaps: dict[str, pd.DataFrame] = {}
    for m in MONTHS:
        got = None
        # 平日だけを候補にする（土日を叩くとクォータの無駄）
        for day in pd.bdate_range(m, m + pd.Timedelta(days=9))[:4]:
            got = fetch_snapshot(cli, day)
            if got is not None and len(got):
                snaps[f"{day:%Y-%m-%d}"] = got
                print(f"  {day:%Y-%m-%d}: {len(got)} 銘柄", flush=True)
                break
        if got is None:
            print(f"  {m:%Y-%m}: 取得できず", flush=True)
        time.sleep(SLEEP)

    if len(snaps) < 4:
        print(f"スナップショットが {len(snaps)} 個しか取れなかった。中断する。")
        return

    dates = sorted(snaps)
    first, last = dates[0], dates[-1]
    print(f"\nスナップショット {len(snaps)} 個: {first} 〜 {last}\n")

    # --- 銘柄ごとに「最初の価格」「最後の価格」「最後に現れた日」を集計 ---
    rec: dict[str, dict] = {}
    for d in dates:
        df = snaps[d]
        df = df[df["AdjC"].notna() & (df["AdjC"] > 0)]
        for code, px in zip(df["Code"].astype(str), df["AdjC"].astype(float)):
            r = rec.setdefault(code, {"first_px": px, "first_date": d})
            r["last_px"], r["last_date"] = px, d

    # --- 投資対象ユニバース（ETF・TOKYO PRO MARKET を除く）で絞る ---
    master = cli.get_eq_master(date=first.replace("-", ""))
    master["Code"] = master["Code"].astype(str)
    inv = master[(master["MktNm"].isin(INVESTABLE_MKT)) & (master["S33Nm"] != "その他")]
    universe = set(inv["Code"]) & set(rec)

    rows = []
    for code in universe:
        r = rec[code]
        if r["first_date"] != first:
            continue  # 期首に居なかった銘柄（新規上場）は対象外
        rows.append({
            "code": code,
            "group": "生存" if r["last_date"] == last else "消滅",
            "return_pct": (r["last_px"] / r["first_px"] - 1) * 100,
            "last_date": r["last_date"],
        })

    df = pd.DataFrame(rows)
    df = df.merge(inv[["Code", "CoName", "MktNm"]], left_on="code", right_on="Code", how="left")
    df.drop(columns=["Code"]).to_csv(OUT, index=False, encoding="utf-8")

    # --- 結果 ---
    print("=" * 70)
    print(f"期首({first})に上場していた投資対象: {len(df)} 銘柄")
    print()
    for g in ["生存", "消滅"]:
        s = df[df["group"] == g]["return_pct"]
        if len(s):
            print(f"{g} (n={len(s):4d}, {len(s) / len(df) * 100:4.1f}%): "
                  f"平均 {s.mean():+7.1f}%  中央値 {s.median():+7.1f}%  "
                  f"最小 {s.min():+8.1f}%  最大 {s.max():+8.1f}%")

    gone = df[df["group"] == "消滅"]["return_pct"]
    surv = df[df["group"] == "生存"]["return_pct"]
    if len(gone) and len(surv):
        all_mean = df["return_pct"].mean()
        print()
        print(f"全銘柄の平均リターン            : {all_mean:+.2f} %")
        print(f"生存銘柄だけの平均リターン      : {surv.mean():+.2f} %")
        print(f"→ **サバイバーシップバイアス   : {surv.mean() - all_mean:+.2f} ポイント**")
        print(f"   （{first} 〜 {last} の約{len(MONTHS) / 12:.0f}年間）")
        print()
        worst = df.nsmallest(8, "return_pct")[["code", "CoName", "group", "return_pct", "last_date"]]
        print("最も下落した8銘柄:")
        print(worst.to_string(index=False))

    print(f"\n保存: {OUT}")


if __name__ == "__main__":
    main()
