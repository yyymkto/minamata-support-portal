---
title: "🆕 「最新のお知らせ」にストック制度の候補があります"
labels: ストック情報要確認
---

{{ env.RUN_DATE }} の定期監視（毎週）で、「最新のお知らせ」（`life_info.json`）の
直近1週間分の中から、ストック側（`child_support_base.json` /
`young_adult_support_base.json`）に関連しそうな記事を検知しました
（{{ env.FINDINGS_COUNT }} 件）。

このスクリプト（`scripts/detect_stock_candidates.py`）はストックデータを
自動更新しません。以下の内容を人間またはエージェントが確認し、必要であれば
手動でデータを更新してください。

## 実行ログ
{{ env.RUN_URL }}

## 検知内容

{{ env.REPORT }}

## 対応方法
1. 「既存ストック制度の更新情報らしいもの」は、実際に記事内容を確認し、該当する
   既存項目（`description` / `conditions_note` / `last_verified` 等）を更新してください。
2. 「新しい恒常制度の候補らしいもの」は、一度きりのキャンペーンではなく本当に
   恒常的な制度かを確認したうえで、`child_support_base.json` または
   `young_adult_support_base.json` に新規項目として追加するか検討してください。
   追加する場合は、既存項目と同じスキーマ（`base_id` / `official_url` /
   `how_to_apply` 等）を満たすよう、記事だけでなく制度の公式ページも確認してください。
3. Gemini判定は誤検知・見逃しの両方があり得ます。「これは違う」と判断した項目は
   特に対応不要です。
4. 対応が完了したら、このIssueをクローズしてください。次回以降の監視で再度
   検知した場合は、このIssueが自動的に再オープンされます。
