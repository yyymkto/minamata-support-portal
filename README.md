# みなまた くらしナビ（非公式）

水俣市公式サイト等の情報を自動収集・手動キュレーションし、世代や困りごと別に整理して
届ける非公式の静的サイトです。ビルドステップなしの単一HTML（`src/index.html`）＋
JSONデータという最小構成で、GitHub Pagesでホストしています。

宇城市版「熊本地震 支援情報ナビ」の仕組みをベースにした派生プロジェクトですが、
以下の点が異なります。

- 特定のカテゴリで事前に絞り込まず、新着情報を**丸ごと収集**してからGeminiで判定（取りこぼし防止）
- 「防災・生活再建」ではなく「**世代タグ × ジャンルタグ**」の2軸で分類
- 新着情報（フロー）だけでなく、制度そのものを常時一覧できるページ（ストック）も持つ

## 画面構成：3タブ

| タブ | 位置づけ | 対象 | データソース |
|---|---|---|---|
| 最新のお知らせ | フロー（毎朝自動更新） | 全世代・全立場 | `public/data/life_info.json` |
| 子育て支援制度 | ストック（人手で更新） | 子育て世帯 | `public/data/child_support_base.json` + `concern_mapping.json` |
| 一人暮らし・若者支援 | ストック（人手で更新） | 一人暮らし世帯・若者・移住検討者 | `public/data/young_adult_support_base.json` + `young_adult_concern_mapping.json` |

「子育て支援制度」タブには、子どものステージ・世帯状況を選択すると該当しない制度を
自動的に除外する絞り込み機能があります。選択内容はブラウザのローカルストレージにのみ
保存され、サーバーには送信されません。

```
.
├── .github/
│   ├── workflows/
│   │   ├── daily-update.yml           # 毎朝の新着情報収集＆デプロイ
│   │   └── weekly-monitor.yml         # 毎週のストック情報監視（制度ページの更新・リンク切れ検知）
│   └── workflow-templates/            # 上記2ワークフローが起票するIssueのテンプレート
├── docs/agent-notes/                  # AIエージェント向けの運用状況メモ（人間の閲覧は任意）
├── scripts/
│   ├── update_data.py                 # 新着情報の収集〜Gemini判定〜JSON更新
│   ├── monitor_stock.py               # ストック情報（制度ページ）の更新・リンク切れ監視
│   └── requirements.txt
├── public/data/
│   ├── life_info.json                 # 新着情報データ本体（自動更新される）
│   ├── child_support_base.json        # 子育て支援制度データ（人手で更新）
│   ├── concern_mapping.json           # 子育て支援制度の困りごと分類の定義
│   ├── young_adult_support_base.json  # 一人暮らし・若者支援制度データ（人手で更新）
│   ├── young_adult_concern_mapping.json # 同上の困りごと分類の定義
│   ├── _checked_urls.json             # 自動生成（Gemini判定済みURLの記録、UIからは不参照）
│   └── _monitor_state.json            # 自動生成（ストック監視の内部状態、UIからは不参照）
├── SPEC.md                            # サイトの現状仕様（詳細版）
└── src/index.html                     # フロントエンド（3タブ・世代/困りごとの絞り込みUI）
```

## セットアップ手順

### 1. GitHub リポジトリの準備
1. このフォルダの中身を新しいGitHubリポジトリの直下に配置してpush
2. Settings → Pages → `Source` を **GitHub Actions** に設定

### 2. Gemini API キーの登録
1. [Google AI Studio](https://aistudio.google.com/) でAPIキーを発行
2. Settings → Secrets and variables → Actions → New repository secret
   - Name: `GEMINI_API_KEY`
   - Secret: 発行したキー

### 3. 動作確認
Actions タブ → 「水俣市くらしナビ 自動更新・デプロイ」→ Run workflow で手動実行できます。

## 新着情報の自動収集（scripts/update_data.py）

1. 以下の5ソースから新着記事を**丸ごと**収集します（カテゴリでの事前絞り込みはしません）。
   - 水俣市 新着情報（`https://www.city.minamata.lg.jp/new_list.html`） — 全ジャンル横断
   - 水俣市 防災サイトのお知らせ（`https://www.city.minamata.lg.jp/bousai/list00500.html`）
   - 水俣市 スポーツ情報（RSS）
   - みなまた観光物産協会 イベント情報
   - みなまた観光物産協会 お知らせ
2. 1件ずつGemini APIに渡し、
   - 住民・事業者の生活に関係する情報かどうかを判定
     （入札公告・監査結果など、内部事務手続きのみのものは除外）
   - 該当する**世代タグ**（子育て・妊娠期／学生・若者／現役世代／高齢者／障がいのある方／
     事業者向け／全世代）を複数付与
   - 該当する**ジャンルタグ**（子育て、健康・医療、介護・福祉、税金・年金、住まい、防災・災害、
     イベント、就労・仕事、くらしの手続き、教育・生涯学習、募集・相談、産業・事業支援、交通、環境）を複数付与
   - 要約を生成
3. `source_url` をキーに既存データとマージし、`public/data/life_info.json` を更新します。
4. 判定済みURLは `public/data/_checked_urls.json` に記録し、2回目以降は新着分のみを
   Gemini判定にかけます（API消費の削減、および取りこぼし防止の両立）。

### 収集ソースの追加
水俣市サイトには環境サイト・スポーツサイト・水俣病資料館など複数のサブサイトがあり、
メインの新着情報に一部転載されていますが、専用サイトの方が更新が早い場合があります。
`scripts/update_data.py` の `SOURCES` にサブサイトの一覧ページを追記すれば、同様に収集対象へ
追加できます。

## ストック情報の監視（scripts/monitor_stock.py）

`child_support_base.json` / `young_adult_support_base.json` に載せている制度ページは
人手でしか更新できないため、内容の「サイレントな更新」やリンク切れに気づけません。
`weekly-monitor.yml`（毎週月曜8:00 JST）が各制度ページの最終更新日・本文を確認し、
変化があればIssueを自動起票します。データ自体は自動更新せず、通知のみを行います。

## エラーの自動検知（Issue起票）
`GEMINI_API_KEY` 未設定時や、全ソースへのアクセス失敗時には、GitHub Actionsが自動でIssueを
起票します（宇城市版と同じ仕組み）。「その日は新着が0件だった」だけでは異常とみなしません。

## 注意事項
- 本サイトは非公式のまとめサイトです。**申請前には必ず一次情報（公式サイト）を確認する**旨を
  UI上に明記しています。
- 水俣市サイトの利用規約・robots.txtに従い、過度な高頻度アクセスは避けてください
  （1日1回、各ソースへのリクエスト間に1秒のウェイトを入れています）。
