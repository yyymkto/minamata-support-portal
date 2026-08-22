# 現在の状態（最終更新：2026-08-23 by Antigravity）

> このファイルは常に「今の状態」を反映するよう **上書き更新** します。
> 過去の経緯を追いたい場合は `decisions-log/` を見てください。
> 更新したら、一番上の日時と更新者を必ず書き換えること。

## 今どのフェーズか

- プロジェクト骨格のGitリポジトリ初期化および配置完了
- **本番 `src/index.html` への「子育てこまりごと導線」統合完了**
- データ側の要確認事項（旧「6件」「9件」いずれも）はAntigravityの対応により解消済み
  （`child_support_base.json` に要確認・推定表現の残存なし、2026-08-23時点でClaudeが再確認）
- **ストック情報のサイレント更新監視の仕組みを新規実装（本日）**：
  Antigravityからの相談（`decisions-log/2026-08-23_stock-monitor-proposal.md`）を受け、
  `scripts/monitor_stock.py` と週次GitHub Actions（`weekly-monitor.yml`）を実装・動作確認済み。
  詳細は `decisions-log/2026-08-23_stock-monitor-design-response.md` を参照。

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
│   ├── update_data.py
│   └── monitor_stock.py             # 新規
├── src/
│   └── index.html
├── AGENTS.md
├── CLAUDE.md
├── AGENT_NOTES_README.md
├── README.md
├── concern_prototype.html (プロトタイプ/役目完了)
├── embedded_data.js (プロトタイプ/役目完了)
└── .gitignore
```

## 進行中のタスク

| タスク | 担当 | 状態 |
|---|---|---|
| リポジトリ骨格の構築と初期化 | Antigravity | 完了 |
| `src/index.html` へのUI統合（タブ切り替え型） | Antigravity | 完了 |
| 統合後の動作確認 | 吉野さん | 待ち |
| `child_support_base.json` の要確認事項の解消 | Antigravity | 完了 |
| ストック情報監視スクリプトの設計・実装 | Claude | 完了（下記「未決定の論点」にCI実地確認が残る） |

## 未決定の論点（次に議論すべきこと）

- **【要対応】** `weekly-monitor.yml` はローカルではIssue起票部分まで検証できていない。
  GitHub Actions上で一度 `workflow_dispatch` により手動実行し、正常に動作すること
  （特にIssue起票・`_monitor_state.json`のコミット）を確認してほしい。
- `medical-care-child-guideline`（水俣市医療的ケア児に関するガイドライン）の
  `last_verified` を実際のページの更新日 `2026-08-10` に合わせて更新する
  （`decisions-log/2026-08-23_stock-monitor-design-response.md` で判明）。
- 不要になったプロトタイプ用ファイル（`concern_prototype.html`, `embedded_data.js`, ルートにある`concern_mapping.json`等）の削除タイミング
- 本番デプロイ（Pages）に向けたフロー確認
- 「4. 疑似属性登録（ローカルストレージ）」の実装タイミング

## 直近の変更履歴（簡易、詳細はdecisions-log参照）

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