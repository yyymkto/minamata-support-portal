# 現在の状態（最終更新：2026-08-25 (2) by Claude）

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
│       ├── _monitor_state.json      # 自動生成（監視の内部状態、UIからは不参照）
│       └── _checked_urls.json       # 新規・自動生成（Gemini判定済みURLの記録、UIからは不参照）
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
| 疑似属性登録（ローカルストレージ）機能 | Antigravity（実装）→Claude（レビュー・バグ修正） | 完了 |

## 未決定の論点（次に議論すべきこと）

- **【要対応・最優先】** Gemini APIの429応答本文を確認したところ、
  `"Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests,
  limit: 500, model: gemini-3.5-flash-lite"` と明記されており、**無料枠の1日500リクエスト上限
  (RPD) に達したことが確定**した（2026-08-24に手動実行を繰り返した累積によるもの、
  自業自得）。サーキットブレーカーは意図通り動作し、3件連続失敗した時点（5〜6件目）で
  即座に打ち切って既存データを保持したまま正常終了しており、数時間ジョブが
  ハングする事態は回避できた。ただし応答本文の"Please retry in X秒"がいずれも
  数秒〜数十秒と短いことから、固定時刻での一斉リセットではなく**過去24時間の
  リクエスト数によるローリングウィンドウ**の可能性がある（未確認）。
  **手動実行でのこれ以上の検証は上限に阻まれ意味がないため、いったん打ち切り、
  翌日以降の定期実行（毎朝7:00 JST）で自然に回復した状態からの動作を確認するのがよい。**
  確認ポイント：(1) `life_info.json` のtitleが正しい日本語になること、
  (2) 429がほぼ出ずに最後まで完走すること、(3) 2回目以降の実行で「判定済みとしてスキップ」
  件数が増えていること。今リポジトリに入っている `life_info.json` は、
  文字化け＆429対応より前の状態のまま（既存80件のみ）。
- 本番デプロイ（Pages）に向けたフロー確認

## 直近の変更履歴（簡易、詳細はdecisions-log参照）

- 2026-08-25: [Claude] 吉野さんが共有した最新の実行ログで、429の応答本文に
  `"Quota exceeded for metric: .../generate_content_free_tier_requests, limit: 500,
  model: gemini-3.5-flash-lite"` と明記されているのを確認し、RPD（1日500リクエスト）
  上限到達を確定させた（前回のchangelogでは「可能性が高い」の推測段階だった）。
  同ログで、直前に追加したサーキットブレーカーが本番で意図通り動作し、5〜6件目で
  3件連続失敗を検知して即座に打ち切り・既存データ保持のまま正常終了したことも確認できた
  （数時間ハングする事態を実際に回避できた）。あわせて、"Please retry in X秒"の値が
  いずれも数十秒以内と短いことから、固定時刻の一斉リセットではなくローリングウィンドウの
  可能性がある点をCURRENT_STATUS.mdに追記（未確認の推測として明記）。
- 2026-08-25: [Claude] 吉野さんが共有した実行ログで、RPM対応（4.5秒間隔）後も
  134件目以降、リトライ上限まで使い切ってなお毎回429で失敗し続ける事象を発見。
  前回（8/24）の「回復と失敗を繰り返す」パターンと異なり**全く回復しない**ことから、
  RPM（分単位）ではなく **RPD（1日の上限）を使い切った** 可能性が高いと判断
  （8/24中に手動実行を繰り返した累積と推定。日本時間の翌16-17時頃にリセットされる想定）。
  対応: (1) 429失敗時にGeminiの応答本文をログに残すようにし、次回以降どの上限
  （RPM/RPD/TPM等）か一目で分かるようにした。(2) リトライを使い切ってなお失敗する状態が
  `GEMINI_CONSECUTIVE_FAILURE_LIMIT`（デフォルト3）件連続したら、残りの候補判定を
  打ち切ってここまでの結果を保存する「サーキットブレーカー」を追加。これにより、
  恒久的な上限到達時に数時間かけて全候補のリトライを無駄にすり潰す事態を防止する
  （失敗した候補は`_checked_urls.json`に記録されないため、次回また判定される）。
  モックで「2件成功→3件連続失敗で打ち切り→成功した2件のみ保存」を検証済み。
- 2026-08-24: [Claude] 吉野さんが再実行したところ「変更をコミット・push」ステップが
  `! [rejected] main -> main (fetch first)` で失敗し、そのrunで集めたデータが
  どこにも反映されずロストする不具合を発見。原因は、Gemini判定の重複排除追加で
  ジョブの実行時間が十数分に伸びたため、その間に他のpush（手動再実行や
  weekly-monitor.yml）が先にmainへ反映されると、単純な`git push`が
  non-fast-forwardで失敗するようになったこと（`daily-update.yml`・
  `weekly-monitor.yml`は元々どちらもリトライなしの単純pushだった）。
  両ワークフローの「コミット・push」ステップに、失敗時は`git pull --rebase`
  してから再試行するループ（最大5回、リトライ間隔を漸増）を追加した。
- 2026-08-24: [Claude] 吉野さんから「初回実行だから候補数が多いだけで、次回以降は減るか」と
  質問があり、コードを確認したところ**減らない**ことが判明。`collect_all_candidates()`は
  毎回、新着一覧ページに今載っている項目をまるごと取得するだけで、「前回判定済みのURLは
  スキップする」処理がどこにもなかった（`merge_items()`はGemini判定後の`life_info.json`
  反映時にしか使われない）。特にナビゲーションメニュー等の恒久的に無関係なリンク
  （候補の半数近くを占める）が毎回律儀に再判定されており、429の主因の一つでもあった。
  対応として、Gemini判定済みURL（採用・不採用いずれも。API呼び出し自体が失敗したものは
  含めない）を`public/data/_checked_urls.json`に記録し、次回以降はそのURLを候補から
  除外してGemini呼び出し自体をスキップするようにした。`daily-update.yml`も
  `_checked_urls.json`をコミット対象に追加。モックで「1回目は3件中3件判定・2回目は
  同じ3件が全てスキップされ0件判定」「API失敗した候補は次回また判定される」の両方を
  検証済み。
- 2026-08-24: [Claude] 吉野さんがAI Studioのレート制限画面（実アカウント）を確認したところ、
  `gemini-3.5-flash-lite`（無料枠）は **RPM 15（超過して赤表示）、RPD 500（143/500で余裕あり）**
  だった。直前の対応（候補間隔1.2秒）はRPM換算で約50リクエスト/分となり、まだ15 RPMを
  大きく超えていたため、429の頻発は解消しきれない見込みだった。日をまたぐのを待つ必要は
  なく（RPDには余裕があるため）、単純にペースを15 RPM以内に収める（4秒間隔以上）ことが
  対策と判明。`GEMINI_REQUEST_INTERVAL_SEC` のデフォルトを1.2秒→4.5秒に変更し、
  リトライ処理は「ペース調整をすり抜けた分の保険」という位置づけに。実行時間は候補221件で
  約17分程度に伸びる見込みだが、`daily-update.yml`のジョブに`timeout-minutes`指定はなく
  デフォルト6時間のため問題ない。
- 2026-08-24: [Claude] 吉野さんが実行ログを共有してくれたところ、Gemini API呼び出しが
  大量に `429 Too Many Requests`（レート制限）で失敗していることが判明。問題は、
  レート制限で失敗した候補が「関係ない記事」と同じ扱いで**黙って捨てられていた**こと
  （structure_candidateはAPI失敗時もNoneを返すため区別がつかない）。`call_gemini()` に
  429専用の再試行処理を追加：`Retry-After`ヘッダーがあればそれに従い、なければ
  指数バックオフ（2秒→4秒→8秒…上限30秒、最大5回）で再試行する。あわせて候補間の
  待機時間を0.5秒→1.2秒に広げ、そもそもレート制限に達しにくくした。モック化した
  `requests.post`で「429×2回→成功」「429を上限まで繰り返す→Noneで正常終了」の
  両パターンを検証済み。
- 2026-08-24: [Claude] 直前の文字化け修正（`apparent_encoding`での上書き）をpush後、
  吉野さんが再度`daily-update.yml`を手動実行したところ、**文字化けが直っていなかった**。
  調査の結果、`resp.apparent_encoding`（chardet/charset_normalizerによる推定）が
  ローカル環境（Windows）では正しく"UTF-8-SIG"と判定される一方、GitHub Actions
  （Linux）環境では別の判定結果になっていたらしいと判明（正確な原因はCIログにアクセス
  できないため特定しきれていない）。生バイトを直接確認し、水俣市サイトが例外なく
  UTF-8（先頭にBOM付き）で配信されていることを確認済みだったため、推定に頼るのをやめ、
  `update_data.py`・`monitor_stock.py`双方の`http_get()`で`"utf-8-sig"`に固定する形に
  変更。ローカルで文字化け（置換文字U+FFFD）が0件になることを確認済み。
- 2026-08-24: [Claude] 吉野さんが`daily-update.yml`を手動実行（workflow_dispatch）したところ、
  Gemini判定自体は正常に動作し新しい記事が`life_info.json`に反映されるようになった一方で、
  **サイト上のタイトル表示が文字化けする不具合**を発見。原因は `scripts/update_data.py` の
  `http_get()` で、水俣市サイトがレスポンスヘッダーにcharsetを明示しないため`requests`が
  `ISO-8859-1`に誤判定し、スクレイピングしたタイトル文字列が化ける現象。
  `monitor_stock.py`（2026-08-23対応）と全く同じ原因だったが、当時は`update_data.py`側への
  横展開ができていなかった。同じ`apparent_encoding`上書き処理を`http_get()`に追加して修正し、
  実際に`new_list.html`から取得したタイトルが正しい日本語で取得できることを確認済み。
  この不具合はスクレイピング機能自体は当初から持っていたが、これまでの実行はGemini側の
  モデル廃止でずっと0件判定だったため表面化していなかった、という経緯。
- 2026-08-24: [Claude] 吉野さんの依頼でAntigravity実装の「疑似属性登録」機能をレビュー。
  未コミットの `src/index.html` の差分を精査したところ、**2件の致命的なバグ**を発見・修正した。
  (1) JSが参照する `#profile-tags`（プロフィールタグの表示先）がHTML側に存在せず、
  `renderProfileTags()` が `null.innerHTML` への代入でクラッシュし、**新着情報タブを含む
  サイト全体が「データの読み込みに失敗しました」というエラー画面になる**状態だった。
  該当要素を `#view-concern` のヘッダー内に追加。
  (2) `concernSectionHTML()` がプロフィールでの絞り込み結果（`concern._filteredService` /
  `_filteredGuide`）を参照していたが、それらを計算する処理がどこにも実装されておらず、
  常に `undefined` の `.map()` 呼び出しでクラッシュしていた。`child_stage`/`household_type`
  が空配列の項目（誰にでも関係する制度）は常に表示し、プロフィールと合致しない項目のみ
  除外する `itemMatchesProfile()` を実装して復旧。あわせて、絞り込みの結果すべての項目が
  消えたカテゴリ・見出しだけの空セクションを表示しない対応、全カテゴリが空になった場合の
  代替メッセージも追加。
  修正後、jsdom＋実データ（`public/data/*.json`）で実際にページを読み込み、
  タグクリックによる絞り込み・フィードタブへの切り替えまで含めて例外が出ないことを確認済み。
  なお、リポジトリ直下に未追跡の `tags.txt`（作業中の一時ファイルと思われる）が残っている
  点は削除せず、上の「未決定の論点」に記載するに留めた。
- 2026-08-24: [Antigravity] 「使える制度をさがす」タブに「疑似属性登録（ローカルストレージ保存）」のUIを実装（バグの詳細は直後のClaudeの記録を参照）。
- 2026-08-23: [Claude] Antigravityの調査（Gemini API呼び出しが大量に404で失敗）を受け、
  原因を一次情報（`ai.google.dev`公式ドキュメント、および無認証での実エンドポイント疎通確認）
  で裏取り。`gemini-2.0-flash` がGoogle側で廃止済み（404）であること、後継の
  `gemini-3.5-flash-lite` が実在し安定版であることを確認した上で、
  `scripts/update_data.py` の `GEMINI_MODEL` デフォルト値を `gemini-2.0-flash` から
  `gemini-3.5-flash-lite` に変更。なお、Antigravityが挙げていた具体的な廃止日
  （2026年6月1日等）は二次情報源のみで、公式ページでは確認できなかった点に留意。
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