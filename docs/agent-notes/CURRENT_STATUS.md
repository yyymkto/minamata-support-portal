# 現在の状態（最終更新：2026-08-23 (4) by Claude）

> このファイルは常に「今の状態」を反映するよう **上書き更新** します。
> 過去の経緯を追いたい場合は `decisions-log/` を見てください。
> 更新したら、一番上の日時と更新者を必ず書き換えること。

## 今どのフェーズか

- プロジェクト骨格のGitリポジトリ初期化および配置完了
- **本番 `src/index.html` への「子育てこまりごと導線」統合完了**
- データ側の要確認事項（旧「6件」「9件」いずれも）はAntigravityの対応により解消済み
- **ストック情報のサイレント更新監視の仕組みを実装済み**：
  `scripts/monitor_stock.py` と週次GitHub Actions（`weekly-monitor.yml`）。
  詳細は `decisions-log/2026-08-23_stock-monitor-design-response.md` を参照。
- **`update_data.py` の収集ソースに3件追加（本日）**：
  みなまた観光物産協会（イベント情報・お知らせ）、水俣市スポーツ情報（RSS）を追加。
  吉野さんからの指摘・Antigravity提案を受けてClaudeが対象サイトのHTML構造を調査し、
  実装・スクレイピング動作確認まで完了。詳細は
  `decisions-log/2026-08-23_event-sources-response.md` を参照。

## 確定している設計方針

- 収集と表示は分離（動的データ=`life_info.json` ／静的データ=`child_support_base.json`等）
- ストック制度は「カテゴリごとの常時見出し一覧＋上部ジャンプナビ」方式を採用（UIプロトタイプ反映済み）
- `src/index.html` での統合は「完全タブ切り替え型（案C）」を採用。「最新のお知らせ（フィード）」と「使える制度をさがす」を画面上部のタブで切り替える。

## 現在のファイル構成

```
minamata-support-portal
├── .git/
├── .github/
│   ├── workflows/
│   │   ├── daily-update.yml
│   │   └── weekly-monitor.yml       # 新規（ストック情報の監視、週1回）
│   └── workflow-templates/
│       ├── collection-failure-issue.md
│       └── stock-update-detected-issue.md  # 新規
├── docs/
│   └── agent-notes/
│       ├── CURRENT_STATUS.md
│       └── decisions-log/
├── public/
│   └── data/
│       ├── life_info.json
│       ├── child_support_base.json
│       ├── concern_mapping.json
│       └── _monitor_state.json      # 新規・自動生成（監視の内部状態、UIからは不参照）
├── scripts/
│   ├── requirements.txt
│   ├── update_data.py               # SOURCESに3件追加（観光物産協会×2、スポーツRSS）
│   └── monitor_stock.py
├── src/
│   └── index.html
├── AGENTS.md
├── CLAUDE.md
├── AGENT_NOTES_README.md
├── README.md
└── .gitignore
```

（`concern_prototype.html` / `embedded_data.js` は役目を終えたためAntigravityが削除済み）

## 進行中のタスク

| タスク | 担当 | 状態 |
|---|---|---|
| リポジトリ骨格の構築と初期化 | Antigravity | 完了 |
| `src/index.html` へのUI統合（タブ切り替え型） | Antigravity | 完了 |
| 統合後の動作確認 | 吉野さん | 待ち |
| `child_support_base.json` の要確認事項の解消 | Antigravity | 完了 |
| ストック情報監視スクリプトの設計・実装 | Claude | 完了（下記「未決定の論点」にCI実地確認が残る） |
| イベント系収集ソース（観光物産協会・スポーツ）の追加 | Claude | 完了（下記「未決定の論点」にCI実地確認が残る） |

## 未決定の論点（次に議論すべきこと）

- **【要対応】** `weekly-monitor.yml` はローカルではIssue起票部分まで検証できていない。
  GitHub Actions上で一度 `workflow_dispatch` により手動実行し、正常に動作すること
  （特にIssue起票・`_monitor_state.json`のコミット）を確認してほしい。
- **【要対応】** `update_data.py` に追加した3ソース（観光物産協会×2、スポーツRSS）は、
  スクレイピング段階の動作のみローカル確認済み。`GEMINI_API_KEY` がローカルにないため、
  Gemini判定を経て実際に `life_info.json` に反映されるところまでは未検証。
  次回の `daily-update.yml` 実行（または `workflow_dispatch` 手動実行）で確認してほしい。
- 本番デプロイ（Pages）に向けたフロー確認
- 「4. 疑似属性登録（ローカルストレージ）」の実装タイミング

## 直近の変更履歴（簡易、詳細はdecisions-log参照）

- 2026-08-23: [Claude] 吉野さんからの依頼でAPIキーの取り扱いをセキュリティ観点で確認。
  リポジトリがGitHub上でpublicであることを確認した上で、git履歴・現行コードともに
  キーのハードコードや漏洩はなかったが、`scripts/update_data.py` がGemini APIキーを
  URLクエリパラメータ（`?key=...`）で渡しており、API呼び出し失敗時の例外メッセージ
  経由でログにキーが乗りうる構造だったため、公式にサポートされている
  `x-goog-api-key` ヘッダー方式に変更。あわせて `.gitignore` に `.env` 系パターンを
  追加（今のところ実害なし、将来のローカル開発向けの予防策）。
- 2026-08-23: [Claude] Antigravityからの相談（イベント系収集ソース追加案）に回答し、実装まで完了。
  みなまた観光物産協会サイト（Joomla製）の実HTMLを調査し、`.article-header a` セレクタで
  ノイズなく記事一覧が取得できることを確認。水俣市スポーツ情報はRSSフィード
  （`sports/new_list.xml`）が存在したためRSSとして追加。`update_data.py` の `SOURCES` に
  計3件追加し、スクレイピング段階の動作をローカルで確認済み（Gemini判定以降は未検証）。
  詳細は `decisions-log/2026-08-23_event-sources-response.md` 参照。
- 2026-08-23: [Antigravity] 不要になったプロトタイプファイル群を削除。また medical-care-child-guideline の日付更新は直前のスクリプト実行で既に完了済みであることを確認。
- 2026-08-23: [Claude] Antigravityからの相談（monitor_stock.pyの設計案）に回答し、実装まで完了。
  水俣市サイトの実HTMLを確認し、`<time datetime>` 属性から最終更新日を取得する方式を採用
  （本文ハッシュ比較は日付が取れない場合のフォールバックのみに限定）。
  `scripts/monitor_stock.py` を全27件の実URLに対してローカル実行し、抽出日付が既存の
  `last_verified` と全件一致することを確認済み。詳細は
  `decisions-log/2026-08-23_stock-monitor-design-response.md` 参照。
  あわせて、`child_support_base.json` の要確認事項がAntigravityの対応により
  （旧「6件」「9件」いずれも）解消済みであることを再確認。
- 2026-08-22: [Claude] 「要確認6件の解消待ち」の記述が古い誤記だったことが判明したため修正。
  実際は2026-08-15時点で完了済み（`CURRENT_STATUS_SAMPLE.md`参照）。
  代わりに`child_support_base.json`内に別途残っている要確認9件を明記。
- 2026-08-22: [Antigravity] リポジトリの初期化。骨格ファイルの配置および本番 `src/index.html` へのプロトタイプ統合（タブ切り替え型）完了。
- 2026-08-22: [Antigravity] リサーチエージェントを利用し、child_support_base.jsonの要確認9件の調査とデータ反映（上書き）を完了。旧URLが404となっていた出産・子育て応援給付金を削除。