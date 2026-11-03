# SSD Price Tracker

VGA 版 (gpu-tracker) と同じ構成。kakaku.com → 商品ページの正規代理店最安値 → prices.json → dashboard.html。

## ファイル
| ファイル | 役割 |
|---|---|
| scraper.py | スクレイプ、分類、イベント検出、メール、OneDriveバックアップ、git push |
| inject_data.py | data/prices.json を dashboard_template.html に埋め込み dashboard.html を生成 |
| dashboard_template.html | ダッシュボード本体（`__PRICES_DATA__` が置換される） |

## 初回セットアップ（新 repo `ssd-tracker`）
1. 3ファイルを新フォルダに置く（`data/` `logs/` は自動作成）
2. `git init` → GitHub に `ssd-tracker` repo 作成 → push → Settings > Pages を有効化
3. `pip install requests beautifulsoup4`
4. **必ず先に実行**: `python scraper.py --discover` と `python scraper.py --selftest`（下記「要確認」参照）
5. `python scraper.py` で初回実行
6. Task Scheduler（1日2回、例）:
   `schtasks /create /tn "SSD Tracker AM" /sc daily /st 09:00 /tr "python C:\path\ssd-tracker\scraper.py"`
   `schtasks /create /tn "SSD Tracker PM" /sc daily /st 18:00 /tr "python C:\path\ssd-tracker\scraper.py"`
   メール通知を使う場合は環境変数 `SSD_TRACKER_EMAIL_PASSWORD` に Gmail アプリパスワードを設定

## 要確認（価格.comに接続できない環境で作ったため未検証）
- **Gen5 / SATA のインターフェースID**: `--discover` で `pdf_Spec101=xx` を確認し、`LISTING_SOURCES` に追記。
  未設定でも M.2 2280 全体を取得してスペック文字列から Gen4/Gen5 を判定するので動作はするが、ページ数が増える。
- **一覧ページ構造**: VGA版と同じ `a[href*='/item/K']` ＋ 行(`tr`)のスペック文字列を前提。`--selftest` で
  分類結果（brand/Gen/容量/ヒートシンク判定）が正しいか目視確認。
- **ページ送り** `pdf_pg=N` は推定。`--selftest` で2ページ目以降が取れるか確認。
- **エンタープライズ検索** `kakaku.com/search_results/<keyword>/` も推定。取得ゼロなら `ENTERPRISE_SEARCH_KEYWORDS` / URL を調整。
- ヒートシンク判定は名称・スペック内のキーワード方式。誤判定は `HEATSINK_OVERRIDE = {"K000...": True/False}` で固定。

## 主な設定（scraper.py 冒頭）
- `ALERT_PCT = 0.05`（前回比 ±5%）、`ALERT_PCT_7D = 0.10`（7日比 ±10%）。dashboard側にも同値あり（`ALERT_PCT` / `ALERT_PCT_7D`）
- `CONSUMER_BRANDS` / `ENTERPRISE_BRANDS`、`SATA_BUCKETS` / `M2_BUCKETS`（容量許容範囲: 500G=480–520, 1TB=960–1050 など）
- 除外: Gen3、M.2 SATA、外付け/ポータブル、PS5対応（`EXCLUDE_*`）
- `DETAIL_HISTORY_DAYS = 30`: これより古い履歴はモデル別一覧を削除しサマリのみ保持（HTML肥大防止）
