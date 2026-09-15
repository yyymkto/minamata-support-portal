# エージェント間情報共有の仕組み：使い方

Claude Code・Antigravity・Gemini（ブラウザ経由）など、複数のAIツールで
このプロジェクトを進めるにあたり、「誰かが一歩古い状態を把握したまま指示を出す」
というズレを防ぐための最小限の仕組みです。

## ファイル構成

```
CLAUDE.md                              ← Claude Codeが自動で読む起点ファイル
AGENTS.md                              ← Antigravityが自動で読む起点ファイル（CLAUDE.mdと同内容）
docs/agent-notes/
├── CURRENT_STATUS.md                  ← 常に最新の状態（上書き運用）
├── design-specs/                      ← Claude（設計）→Antigravity（実装）の受け渡し用
│   ├── TEMPLATE.md                    ← 新しい設計書を書く時のひな形
│   └── YYYY-MM-DD_機能名.md           ← 個別機能の設計書（実装完了後も残す）
└── decisions-log/
    ├── TEMPLATE.md                    ← 新しい決定を記録する時のひな形
    └── YYYY-MM-DD_件名.md             ← 決定の記録（追記専用、削除しない）
```

`design-specs/` と `decisions-log/` の違い：
- `design-specs/` は**実装前**に書く、これから作るものの仕様書（概要・データ構造・画面/API仕様・タスク一覧）。
- `decisions-log/` は**決まった後**に書く、なぜその設計にしたかの記録。

## claude.ai（設計担当）との連携

大きめの機能は、claude.aiのプロジェクト機能でClaudeに設計を依頼し、
`design-specs/TEMPLATE.md`の型で設計書を書いてもらう運用にしている。
claude.aiは直接リポジトリを読めないため、人間が設計書をコピーして
`docs/agent-notes/design-specs/YYYY-MM-DD_機能名.md`として保存する。
Antigravity（実装担当）はこの設計書に従って実装し、**食い違いや不足を見つけても
無断で仕様を変えず、設計書に疑問点を追記した上で作業を止めて報告する**。

## 使い方（3ルールだけ）

### 1. 作業を始める前に `CURRENT_STATUS.md` を読む
Claude Codeなら`CLAUDE.md`経由で自動的に案内されます。
Antigravityや他のツールでも、**最初の一言で「まずCURRENT_STATUS.mdを読んで」と
指示する**運用にしてください。

### 2. 何か決めたら、その場で `CURRENT_STATUS.md` を上書きする
「後でまとめて更新しよう」はほぼ確実に忘れます。決めたその場で、
ファイル冒頭の日時と更新者を書き換えて、該当セクションを直す。

### 3. 大きな決定は `decisions-log/` にも残す
`CURRENT_STATUS.md`は「今」の状態だけなので、**なぜそうなったか**は
すぐ消えてしまいます。あとから「なぜこの設計にしたんだっけ」となりそうな
決定は、`TEMPLATE.md`をコピーして1ファイル追記してください。

## Antigravity側の設定

Antigravityにも同様の「起点ファイル」を自動で読む仕組みがある可能性があります
（例：`AGENTS.md`という名前を使うツールもあります）。もしAntigravityが
`CLAUDE.md`をそのままは読まない場合、同じ内容の`AGENTS.md`をルートに
もう1つ置く（中身は「まずdocs/agent-notes/CURRENT_STATUS.mdを読んでください」
の一言で十分）ことで対応できます。

## Geminiとのやり取りについて

Geminiはブラウザ経由の会話のみで、リポジトリのファイルに直接アクセスできないため、
このファイルを使った自動連携はできません。引き続き、
`docs/agent-notes/CURRENT_STATUS.md`の中身をコピペで共有する運用になります。
ただし、**単一の「今の状態」ファイルを見ればいいだけ**になるので、
これまでのように長い経緯まとめを都度作る必要はなくなるはずです。
