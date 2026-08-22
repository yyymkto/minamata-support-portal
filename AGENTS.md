# このプロジェクトについて（AIエージェント向け）

このファイルは Antigravity（および AGENTS.md 対応の各種ツール）が
作業開始時に自動で読み込みます。Claude Code を使う場合は同内容の
`CLAUDE.md` が同じ役割を果たします。

## 最初に読むもの

1. **`docs/agent-notes/CURRENT_STATUS.md`** ← 必読。プロジェクトの「今」の状態。
   これだけ読めば、他のエージェント（Claude Code / Gemini 等）との
   認識ズレを防げます。
2. 詳しい経緯を知りたい場合のみ `docs/agent-notes/decisions-log/` を参照。

## プロジェクト概要

みなまた くらしナビ（非公式）。水俣市の生活情報・子育て支援制度を、
世代・困りごと別に整理して届ける静的サイト。

- フロントエンド: `src/index.html`（単一HTML + Tailwind CDN + Vanilla JS、ビルドステップなし）
- データ: `public/data/*.json`
- 自動更新: `.github/workflows/daily-update.yml`（新着情報のみ、毎朝7:00 JST）

## 運用ルール

- **状態の変更を伴う作業をしたら、必ず `CURRENT_STATUS.md` を更新すること。**
  更新しないまま作業を終えると、次にこのファイルを読む人（人間もエージェントも）が
  古い情報のまま動いてしまう。
- 大きな設計判断をしたら、`decisions-log/` に日付つきで1ファイル追記する
  （このファイル自体は上書きせず、追記のみ）。
- ファイル形式はMarkdown固定。他のエージェント・人間も読めるように。
