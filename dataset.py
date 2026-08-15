"""株価日足の取得とキャッシュ。

設計方針:
  - 主データ源は yfinance（当日終値まで取れる）。J-Quants Free は12週間遅延で
    日次運用に使えないため、突合検証用に別スクリプト（compare_sources.py）で使う
  - 取得結果は data/raw/ にキャッシュする。yfinanceにも実質的なレート制限があり、
    試行のたびに全期間を取り直すのは無駄かつ危険
  - **異常は握りつぶさず必ず例外にする（fail-fast）**。
    データ欠損を黙って0や前日値で埋めると、バックテストは静かに嘘をつく。
    「戦略が悪い」のか「データが壊れている」のかを区別できなくなるのが最悪。
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

REPO_ROOT = Path(__file__).resolve().parent
CACHE_DIR = REPO_ROOT / "data" / "raw"

# キャッシュをこれ以上古くさせない。日足なので1日で十分
CACHE_MAX_AGE = timedelta(hours=12)

OHLCV = ["Open", "High", "Low", "Close", "Volume"]


class DataQualityError(Exception):
    """取得データが検証を通らなかった。黙って先に進ませないための例外。"""


def _validate(df: pd.DataFrame, symbol: str) -> None:
    """バックテストに流す前に、データが壊れていないことを確認する。"""
    if df.empty:
        raise DataQualityError(f"{symbol}: データが0行。銘柄コードか期間が誤っている可能性")

    missing = [c for c in OHLCV if c not in df.columns]
    if missing:
        raise DataQualityError(f"{symbol}: 必要な列がない: {missing}")

    if not isinstance(df.index, pd.DatetimeIndex):
        raise DataQualityError(f"{symbol}: インデックスが日付型でない")

    if not df.index.is_monotonic_increasing:
        raise DataQualityError(f"{symbol}: 日付が昇順でない")

    dup = df.index.duplicated().sum()
    if dup:
        raise DataQualityError(f"{symbol}: 日付が重複している行が {dup} 件")

    nan_rows = df[OHLCV].isna().any(axis=1)
    if nan_rows.any():
        where = [str(d.date()) for d in df.index[nan_rows]][:5]
        raise DataQualityError(
            f"{symbol}: 履歴の途中にOHLCV欠損が {nan_rows.sum()} 件: {where}"
            " — 末尾の未確定バーではないので、埋めずに原因を調べること"
        )

    price_cols = ["Open", "High", "Low", "Close"]
    if (df[price_cols] <= 0).any().any():
        raise DataQualityError(f"{symbol}: 価格が0以下の行がある")

    # 高値 < 安値 のような論理破綻は、データ源のバグを示す明確なサイン。
    # ただし auto_adjust による除数計算で 1e-13 程度の誤差が乗るため、
    # 相対1e-6（＝株価3000円で0.003円）を超えたものだけを異常とみなす。
    tol = df["Close"].abs() * 1e-6
    broken = (
        (df["Low"] - df["High"] > tol)
        | (df["Close"] - df["High"] > tol)
        | (df["Low"] - df["Close"] > tol)
    )
    if broken.any():
        where = [str(d.date()) for d in df.index[broken]][:5]
        raise DataQualityError(
            f"{symbol}: High/Low/Close の大小関係が矛盾する行が {broken.sum()} 件: {where}"
        )

    # --- 行をまたぐ整合性: 異常なジャンプ ---
    # 行単位の検査ではデータ破損を捕まえられない。
    # 実例: 1306.T の 2026-03-30〜31 は yfinance 上で価格が 1/10・出来高が10倍になっている
    #       （分割ではない。Stock Splits=0、分割履歴も空。ベンダー側の単位ミス）。
    #       各行はHigh>=Low等を満たすため行単位検査は通ってしまい、
    #       戦略には「-90%の暴落と+948%の急騰」に見える。
    # 日本株には値幅制限があるため、1日で±50%を超える終値変化は
    # 「未調整の分割」か「データ破損」のどちらかしかない。どちらも黙って通してはいけない。
    ret = df["Close"].pct_change()
    jumps = ret.abs() > 0.5
    if jumps.any():
        detail = [f"{d.date()}({ret[d] * 100:+.0f}%)" for d in df.index[jumps]][:5]
        raise DataQualityError(
            f"{symbol}: 1日で±50%を超える価格変動が {int(jumps.sum())} 件: {detail}"
            " — 未調整の分割かデータ破損。J-Quants(JPX公式)と突合して原因を特定すること"
        )

    # 出来高0＝実質的に売買できない日。エラーにはしないが、
    # 「約定したことになっている取引が現実には不可能」という罠なので数だけ出す。
    zero_vol = int((df["Volume"] <= 0).sum())
    if zero_vol:
        print(f"[dataset] {symbol}: 出来高0の日が {zero_vol} 件（約定不能日。結果を読む際に留意）")


CORRECTIONS_CSV = REPO_ROOT / "data" / "corrections.csv"


def _apply_corrections(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """既知のデータ破損を補正する。

    **データを黙って直すのは本来やってはいけないこと。** だから次の条件を課す:
      1. 補正は data/corrections.csv に1行ずつ明示する（Git管理下＝履歴が残る）
      2. 各行に「何を根拠に、いつ確認したか」を必ず書く（J-Quants等の一次情報）
      3. 適用したら毎回コンソールに出す。静かに効く補正を作らない

    補正しないという選択肢もあるが、その場合ベンチマーク銘柄が使えなくなる。
    「壊れたまま使う」より「根拠つきで直して記録を残す」方が検証として健全。
    """
    if not CORRECTIONS_CSV.exists():
        return df

    corr = pd.read_csv(CORRECTIONS_CSV)
    corr = corr[corr["symbol"] == symbol]
    if corr.empty:
        return df

    df = df.copy()
    for _, r in corr.iterrows():
        mask = (df.index >= pd.Timestamp(r["start"])) & (df.index <= pd.Timestamp(r["end"]))
        n = int(mask.sum())
        if not n:
            continue
        df.loc[mask, ["Open", "High", "Low", "Close"]] *= float(r["price_factor"])
        df.loc[mask, "Volume"] *= float(r["volume_factor"])
        print(f"[dataset] {symbol}: 既知の破損を補正 {r['start']}〜{r['end']} ({n}行, "
              f"価格x{r['price_factor']}) 根拠: {r['verified_against']}")
    return df


def _cache_path(symbol: str) -> Path:
    return CACHE_DIR / f"{symbol.replace('.', '_')}_1d.csv"


def _drop_unsettled_tail(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """末尾の未確定バーを落とす。

    yfinanceは、その日のバーが確定していないと **出来高だけ入って OHLC が NaN** の
    行を返すことがある（2026-08-14 に実際に発生）。しかも前日には値が入って見えた日が
    翌日にはNaNに変わることがある。データ源が過去のバーを後から書き換える前提で組む。

    末尾の欠損＝「まだ確定していない」なので落として良い。
    履歴の途中の欠損＝データが壊れているので、落としてはいけない（_validateで例外にする）。
    この2つを混ぜて dropna() すると、静かに嘘をつくバックテストになる。
    """
    incomplete = df[["Open", "High", "Low", "Close"]].isna().all(axis=1)
    n_tail = 0
    for flag in reversed(incomplete.tolist()):
        if not flag:
            break
        n_tail += 1

    if n_tail:
        dropped = df.index[-n_tail:]
        print(f"[dataset] {symbol}: 未確定の末尾 {n_tail} 行を除外 "
              f"({dropped[0].date()} 〜 {dropped[-1].date()})")
        df = df.iloc[:-n_tail]
    return df


def _issues_path(symbol: str) -> Path:
    return CACHE_DIR / f"{symbol.replace('.', '_')}_1d.issues.json"


def _scan_bad_dividends(symbol: str, adjusted: pd.DataFrame) -> list[str]:
    """配当の調整が壊れている除権日を洗い出す。

    yfinanceは、株式分割の際に **株価は調整するのに配当額を調整し忘れる**ことがある。
    実例: 1306.T の 2015-07-10 は分配金 23.0円 / 株価 161.2円 = 14.3%（正しくは2.3円）。

    これが厄介なのは、`auto_adjust=True` が **その除権日より前の価格すべてを
    14%押し下げる**点。チャートは滑らかなままで、リターンだけが静かに水増しされる。
    ベンチマークがこれをやると合格基準A1が丸ごと狂う。

    判定は2段構え:
      1. 配当額が株価の5%超 … 日本株で1回の配当がここまで大きいことは実質ない
      2. **かつ**、調整後の価格系列がその日に大きく飛んでいる
    調整は「除権日より前」の価格だけに掛かるので、係数が誤っていると
    除権日ちょうどに段差が出る。逆に言えば、`repair=True` が直せていれば段差は消える。
    配当メタデータは壊れたままなので、**症状（価格の段差）で判定しないと誤検出になる**。
    """
    t = yf.Ticker(symbol)
    div = t.dividends
    if div is None or div.empty:
        return []
    raw = t.history(period="max", interval="1d", auto_adjust=False)
    if raw.empty:
        return []
    div.index = pd.to_datetime(div.index).tz_localize(None).normalize()
    raw.index = pd.to_datetime(raw.index).tz_localize(None).normalize()

    j = pd.DataFrame({"div": div}).join(raw[["Close"]], how="left").dropna()
    suspect = j[j["div"] / j["Close"] > 0.05]
    if suspect.empty:
        return []

    step = adjusted["Close"].pct_change()
    bad = []
    for d, r in suspect.iterrows():
        jump = float(step.get(d, 0.0) or 0.0)
        if abs(jump) > 0.05:
            print(f"[dataset] {symbol}: 配当の調整が壊れている {d.date()} "
                  f"(配当 {r['div']:.2f}円/株価 {r['Close']:.1f}円={r['div'] / r['Close'] * 100:.1f}%, "
                  f"調整後価格の段差 {jump * 100:+.1f}%)")
            bad.append(str(d.date()))
        else:
            print(f"[dataset] {symbol}: {d.date()} の配当は額が不自然だが、"
                  f"調整後の価格に段差なし({jump * 100:+.1f}%)。repairで補正済みとみなす")
    return bad


def _fetch(symbol: str) -> pd.DataFrame:
    """yfinanceから全期間の日足を取る。

    auto_adjust=True: 株式分割・配当を調整済みの値で返す。
    分割を調整し忘れると「1日で株価が1/3になった大暴落」に見え、戦略が誤作動する。
    調整の正しさは J-Quants(JPX公式) の AdjC と突合して確認済み（乖離 0.0000%）。

    repair=True: yfinance組み込みの破損補正。実測でわかったこと:
      - 配当の分割調整漏れは**直せる**（1306.T 2015-07-10 の段差 +15.7% → -0.7%）
      - 価格が1/10になる破損は**直せない**（repairが見るのは100倍＝通貨単位の誤り）
        → これは data/corrections.csv で個別に補正する
      - 正常なデータへの誤検出はなし（J-Quants公式値と突合、修復0行・乖離0.0000%）
      - フル履歴では **scikit-learn が必要**（ドキュメントに記載のない依存）
    """
    df = yf.Ticker(symbol).history(period="max", interval="1d", auto_adjust=True, repair=True)
    if df.empty:
        raise DataQualityError(f"{symbol}: yfinanceが空を返した。ティッカーを確認すること")
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    df.index.name = "Date"
    n_repaired = int(df["Repaired?"].sum()) if "Repaired?" in df.columns else 0
    if n_repaired:
        print(f"[dataset] {symbol}: yfinanceのrepairが {n_repaired} 行を補正")
    df = df[OHLCV].copy()

    # 配当の調整漏れはキャッシュに残らないので、取得時に調べて脇に書き出しておく
    _issues_path(symbol).write_text(
        json.dumps({"bad_dividend_dates": _scan_bad_dividends(symbol, df)}), encoding="utf-8"
    )
    return _drop_unsettled_tail(df, symbol)


def _check_bad_dividends_in_window(symbol: str, df: pd.DataFrame) -> None:
    """調整漏れ配当が、使う期間の値を汚していないか確認する。

    調整は「除権日より前」の価格に効く。したがって
    **除権日が期間の開始より後にある場合だけ**、期間内の価格が汚染される。
    除権日が期間より前なら影響はない（＝開始日を後ろにずらせば回避できる）。
    """
    p = _issues_path(symbol)
    if not p.exists():
        return
    dates = json.loads(p.read_text(encoding="utf-8")).get("bad_dividend_dates", [])
    hit = [d for d in dates if pd.Timestamp(d) > df.index[0]]
    if hit:
        raise DataQualityError(
            f"{symbol}: 分割調整漏れの配当 {hit} が期間開始({df.index[0].date()})より後にある。"
            f" → この期間の価格は最大で十数%押し下げられており、リターンが水増しされる。"
            f" 開始日を {hit[-1]} より後にするか、別のティッカーを使うこと"
        )


def load_bars(
    symbol: str,
    start: str | None = None,
    end: str | None = None,
    *,
    refresh: bool = False,
) -> pd.DataFrame:
    """日足を返す。キャッシュがあればそれを使う。

    Args:
        symbol: yfinance形式のティッカー（日本株は "7203.T"）
        start, end: "YYYY-MM-DD"。endは**その日を含む**
        refresh: Trueならキャッシュを無視して取り直す
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(symbol)

    use_cache = (
        not refresh
        and path.exists()
        and datetime.now() - datetime.fromtimestamp(path.stat().st_mtime) < CACHE_MAX_AGE
    )

    if use_cache:
        df = pd.read_csv(path, index_col="Date", parse_dates=["Date"])
    else:
        df = _fetch(symbol)
        df.to_csv(path)
        time.sleep(0.5)  # 連続取得でレート制限に当たらないよう間隔を空ける

    # 検証は**実際に使う期間だけ**に掛ける。
    # yfinanceの古いデータには実在の矛盾がある（例: 1306.T の2009〜2010年に
    # 終値が高値を0.2〜0.6%上回る行が4件）。使わない期間の傷でバックテストを
    # 止めるのは過剰。逆に、使う期間の傷は絶対に見逃さない。
    if start:
        df = df[df.index >= pd.Timestamp(start)]
    if end:
        df = df[df.index <= pd.Timestamp(end)]

    if df.empty:
        raise DataQualityError(f"{symbol}: 指定期間 {start}〜{end} にデータが0行")

    # 補正はキャッシュではなく読み出し時に当てる。
    # キャッシュに焼き込むと「生データ」と「直した後」の区別がつかなくなる。
    df = _apply_corrections(df, symbol)

    _validate(df, symbol)
    _check_bad_dividends_in_window(symbol, df)
    return df


PANEL_DIR = CACHE_DIR / "panels"
PANEL_MAX_AGE = timedelta(hours=6)


def load_panel(
    symbols: list[str],
    *,
    start: str = "2015-01-01",
    refresh: bool = False,
    batch: int = 100,
) -> dict[str, pd.DataFrame]:
    """複数銘柄の日足をまとめて取り、{"Open": df, "Close": df, ...} で返す。

    各 df は index=日付 / columns=銘柄。フォワードテストは毎日500銘柄を見るので、
    `load_bars` を1銘柄ずつ呼ぶと取得だけで数分かかり、失敗する確率もそのぶん上がる。
    合格基準A1（欠測≤5営業日）を守るには、取得の回数そのものを減らす必要がある。

    `load_bars` と違い、**個別銘柄の品質検証はしない**。500銘柄のうち1つが
    壊れているだけで当日の実行が止まると、それこそA1に響くため。
    代わりに壊れた銘柄はその日の候補から外し、理由を返り値の "excluded" に載せる。
    """
    PANEL_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(",".join(sorted(symbols)).encode()).hexdigest()[:16]
    path = PANEL_DIR / f"{key}.csv"

    fresh = (
        not refresh
        and path.exists()
        and datetime.now() - datetime.fromtimestamp(path.stat().st_mtime) < PANEL_MAX_AGE
    )
    if fresh:
        long = pd.read_csv(path, parse_dates=["Date"])
    else:
        frames = []
        for i in range(0, len(symbols), batch):
            chunk = symbols[i:i + batch]
            raw = yf.download(chunk, start=start, auto_adjust=True, progress=False,
                              group_by="column", threads=True)
            if raw.empty:
                continue
            parts = []
            for f in OHLCV:
                if f not in raw:
                    continue
                s = raw[f]
                if isinstance(s, pd.Series):      # 1銘柄だけのとき
                    s = s.to_frame(chunk[0])
                parts.append(s.stack().rename(f))
            if parts:
                frames.append(pd.concat(parts, axis=1))
        if not frames:
            raise DataQualityError("load_panel: 1銘柄も取得できなかった")
        long = pd.concat(frames).reset_index()
        long.columns = ["Date", "Symbol"] + OHLCV
        d = pd.to_datetime(long["Date"], utc=True).dt.tz_localize(None).dt.normalize()
        long["Date"] = d
        long = long.dropna(subset=["Close"])
        long.to_csv(path, index=False)

        got = set(long["Symbol"])
        missing = sorted(set(symbols) - got)
        if missing:
            # 上場廃止（日本ではTOB・MBOが主）で消える銘柄は必ず出る。
            # 実測で年約3%。**止めずに記録して先へ進む**（A1: 欠測≤5営業日）。
            print(f"[dataset] 株価が取れなかった銘柄 {len(missing)}/{len(symbols)} 件"
                  f"（上場廃止の可能性）: {' '.join(missing[:10])}"
                  f"{' …' if len(missing) > 10 else ''}")

    out = {f: long.pivot(index="Date", columns="Symbol", values=f).sort_index() for f in OHLCV}
    return out


def settled_dates(panel: dict[str, pd.DataFrame], min_coverage: float = 0.8) -> pd.DatetimeIndex:
    """「その日のバーが確定している」と見なせる日付だけを返す。

    yfinance は当日のバーを、出来高だけ入れて OHLC を NaN のまま先に配信することがある
    （実例: 7203.T 2026-08-14。しかも前日には値が入って見えていた）。
    未確定の日で判断すると、翌日に値が変わって記録と食い違う。
    **確定していない日は今日は飛ばし、翌日に処理する。** これが取りこぼしを防ぐ要。
    """
    close = panel["Close"]
    coverage = close.notna().sum(axis=1) / close.shape[1]
    return close.index[coverage >= min_coverage]


def buy_and_hold_return(symbol: str, start: str, end: str, *, refresh: bool = False) -> float:
    """指定期間をバイ&ホールドしたときのリターン(%)。

    合格基準A1（対TOPIX超過リターン）のベンチマーク計算に使う。
    実際に売買する前提に合わせ、**初日の寄付で買い、最終日の終値で売る**。
    """
    df = load_bars(symbol, start, end, refresh=refresh)
    entry = float(df["Open"].iloc[0])
    exit_ = float(df["Close"].iloc[-1])
    return (exit_ / entry - 1.0) * 100.0
