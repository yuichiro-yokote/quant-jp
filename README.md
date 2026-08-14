# quant-jp — 日本株 1年ペーパートレード検証

日本株（日足）を対象に、**架空の資金で1年間フォワードテストを行う**ための検証環境。
実弾は一切使わない。1年後の結果で、実運用に進むかどうかを判断する。

> 設計方針・運用ルール（憲法）・合格基準は OneDrive 側の
> `_Yokote/投資/自動売買/シミュレーション設計_20260814.md` を参照。
> **このリポジトリのコードより、あちらのルールが優先される。**

## セットアップ

```powershell
# 仮想環境（作成済み）
.\.venv\Scripts\Activate.ps1

# J-Quants APIキー
$env:JQUANTS_API_KEY = "取得したAPIキー"

# 疎通確認
python check_env.py       # yfinance（APIキー不要）
python fetch_jquants.py   # J-Quants（APIキー必要）
```

## 構成

| パス | 役割 |
|---|---|
| `check_env.py` | ライブラリとyfinanceの疎通確認 |
| `fetch_jquants.py` | J-Quants API (V2) の疎通確認 |
| `data/raw/` | 取得データのキャッシュ（Git管理外・再取得可能） |
| `strategies/` | 戦略ロジック（backtesting.py の Strategy） |
| `backtest/` | 過去データでの一括検証 |
| `paper/journal/` | **日次の判断・保有・損益。Gitで履歴を固定する** |
| `reports/` | QuantStatsのtear sheet と週次レビュー |

## 使用ライブラリ

| 用途 | ライブラリ | 選定理由 |
|---|---|---|
| データ（正） | `jquants-api-client` | JPX公式。無料プランは直近2年 |
| データ（長期） | `yfinance` | バックテスト用の長期履歴を補完 |
| バックテスト | `backtesting.py` | 現在アクティブにメンテされている |
| 成績評価 | `quantstats` | tear sheet を HTML 出力。pyfolio は非推奨のため不採用 |

**backtrader は 2018年に開発停止しているため採用しない**（日本語記事が多く候補に挙がりやすいが、バグが放置されている）。

## 鉄則

1. 合格基準は開始前に固定する。最優先は **対TOPIXバイ&ホールドで超過リターンがプラスか**
2. **戦略を変更したら1年を数え直す**（調整され続けた記録は検証価値がゼロ）
3. 記録は毎日コミットする（後から書き換えられないことが検証の生命線）
4. **`paper/journal/` を手で編集しない**
