# [2026-08-23] イベント・観光系ニュースソース追加の調査結果・実装（Antigravity提案への回答）

**決めた人/エージェント**: Claude（Antigravityからの相談 `2026-08-23_event-sources-proposal.md` への回答として）
**関係者**: Antigravity、Claude、吉野さん

## 背景・課題

`decisions-log/2026-08-23_event-sources-proposal.md` の通り、市公式サイトの新着情報
フィードに載らない、みなまた観光物産協会・水俣市スポーツ情報のイベント情報が
`life_info.json` に取りこぼされているのではという指摘があり、`update_data.py` の
`SOURCES` への追加調査・実装をClaudeに依頼された。

## 調査結果

### 1. 水俣市スポーツ情報（`city.minamata.lg.jp/sports/`）

RSSフィードが提供されていることを確認（`https://www.city.minamata.lg.jp/sports/new_list.xml`）。
実際に取得したところ4件のエントリが返り、`updated_parsed` に有効な日付
（例: 2026-08-21）が入っていることを確認した。既存の `fetch_rss()` 関数がそのまま
（コード変更なしで）`updated_parsed` をフォールバック利用する実装になっており、
追加のロジック変更は不要だった。

### 2. みなまた観光物産協会（`go-minamata.jp`）

RSSフィードは存在しない（`/feed/` 等はいずれも404）ため、HTML方式で追加。
サイトはJoomla製で、`https://www.go-minamata.jp/news-events/`（イベント）と
`https://www.go-minamata.jp/news-information/`（お知らせ）の各一覧ページを実際に
取得・解析したところ、以下の構造であることを確認した。

```html
<div class="article-header">
  <h2><a href="/news-events/xxxxx.html">記事タイトル</a></h2>
</div>
```

`list_selector=".article-header a"` により、両ページとも各3件・計6件が
ノイズなく取得できることを確認済み（実行ログで検証）。

ページ内には `.com-content-blog__link`（「その他の記事」的な関連記事ウィジェット）
も存在するが、これは掲載時期が不定で「新着」として扱うと季節外れの過去記事を
毎回拾い直すリスクがあるため、意図的に対象外とした
（Antigravityが提案時に懸念していた「ヘッダー・フッターのノイズ」とは別種の
ノイズだが、同じ理由で除外が妥当と判断）。

### 制約事項

- Joomlaのカテゴリブログ表示は1ページあたり3件のみのため、一覧ページの
  スクレイピングでは常に「その時点の最新3件」しか取得できない。日次実行を
  続けることで新着分は取りこぼしなく拾えるが、仮に3件を超えるペースで
  同日中に複数件公開された場合は取りこぼす可能性がある（許容範囲と判断）。
- 一覧ページのタイトルリンク周辺に日付情報がないため、`published_date` は
  `None` になる（既存の他ソースでも同様のケースがあり、`MAX_AGE_DAYS=0`
  （フィルタなし）の現状運用では実害はない）。

## 決定内容

`scripts/update_data.py` の `SOURCES` に以下3件を追加した。

1. `水俣市 スポーツ情報`（RSS、`sports/new_list.xml`）
2. `みなまた観光物産協会 イベント情報`（HTML、`.article-header a`）
3. `みなまた観光物産協会 お知らせ`（HTML、`.article-header a`）

いずれも `collect_all_candidates()` の実行で正常に取得できることをローカルで確認済み
（Gemini API呼び出し前のスクレイピング段階のみ検証。API呼び出し以降のフローは
既存の共通処理のため変更なし）。

## 却下した選択肢

- **関連記事ウィジェット（`.com-content-blog__link`）も収集対象に含める**:
  古い記事を「新着」として繰り返し拾い直すリスクがあるため見送り。
- **`article` タグ全体や `a` タグ全指定などの広い `list_selector`**:
  ナビゲーションメニュー（`sp-menu-item` 等）を大量に拾ってしまうため、
  `.article-header a` まで絞り込んだ。

## 影響を受けたファイル

- `scripts/update_data.py`（`SOURCES` 配列に3件追加。ロジック本体の変更なし）

## 未着手・今後の論点

- 次回の `daily-update.yml` 実行（または `workflow_dispatch` での手動実行）で、
  実際にGemini判定を通過して `life_info.json` に反映されることを確認するとよい
  （ローカル環境に `GEMINI_API_KEY` がないため、このステップは未検証）。
