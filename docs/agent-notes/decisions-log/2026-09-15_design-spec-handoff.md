# [2026-09-15] Claude(設計)→Antigravity(実装)の受け渡しに`design-specs/`を導入

**決めた人/エージェント**: Claude（吉野さんの依頼）
**関係者**: 吉野さん

## 背景・課題

吉野さんが、claude.aiで設計だけを行うClaudeと、実装を行うAntigravityの
分担をより明確にしたいという方針を共有した。既存の`decisions-log/`は
「決まった後」の記録用であり、「これから作るものの仕様」を実装前に
渡すためのフォーマットが無かった。また、実装担当が設計と食い違う点を
見つけた際に無断で仕様を変えてしまうリスクへの対策も明記されていなかった。

## 決定内容

- `docs/agent-notes/design-specs/`を新設。概要／データ構造／画面・API仕様／
  タスク一覧（1タスク半日程度、完了条件つき）／設計判断の理由の型で
  `TEMPLATE.md`を用意した。
- `CLAUDE.md`・`AGENTS.md`双方に「実装前にdesign-specs/を確認する」
  「設計書と食い違う場合は無断で変更せず、疑問点を追記して作業を止めて
  報告する」ルールを追加。
- `AGENT_NOTES_README.md`に、`design-specs/`と`decisions-log/`の役割の違い
  （実装前の仕様 vs 決定後の記録）と、claude.aiとの連携フロー
  （人間がclaude.aiの出力をコピーしてリポジトリに保存する）を追記。

## 却下した選択肢（あれば）

- `decisions-log/`のフォーマットを流用する案 → 決定後の記録と実装前の仕様は
  目的が違う（前者は「なぜ」、後者は「何を」）ため、別フォルダ・別テンプレートとした。

## 影響を受けるファイル

- `CLAUDE.md`
- `AGENTS.md`
- `AGENT_NOTES_README.md`
- `docs/agent-notes/design-specs/TEMPLATE.md`（新規）

---
※ このファイルをコピーして `docs/agent-notes/decisions-log/YYYY-MM-DD_短い件名.md` として保存してください。
※ 決定したら `CURRENT_STATUS.md` の該当箇所も更新すること。
