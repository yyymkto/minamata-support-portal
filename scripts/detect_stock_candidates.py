#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
水俣市 くらしナビ ストック候補検出スクリプト
-------------------------------------------------
「最新のお知らせ」（life_info.json、フロー）の中に、ストック側
（child_support_base.json / young_adult_support_base.json）に載せる価値がある
恒常的な制度の情報が紛れていないかをGeminiで判定し、レポート（Markdown）として
出力する。

判定対象は直近 STOCK_CANDIDATE_LOOKBACK_DAYS 日（デフォルト7日）に収集された項目のみ。
weekly-monitor.yml から週1回実行される想定（scripts/monitor_stock.pyと同じ頻度）。

このスクリプトは child_support_base.json / young_adult_support_base.json を
直接書き換えない。検知結果はIssueとして通知するのみで、実際のデータ反映は
人間またはエージェントが内容を確認したうえで手動で行う方針とする。
理由: これらの静的データは「年1〜数回、手動でメンテナンス」する方針であり、
自動書き換えは誤情報混入のリスクがあるため（decisions-log/
2026-08-28_flow-to-stock-candidate-issue.md 参照）。
また、恒常的な制度が新設される頻度自体が低く、自動書き込みの複雑さに見合わない
という判断もある。

環境変数:
    GEMINI_API_KEY               ... Google AI Studio の Gemini API キー（必須）
    STOCK_CANDIDATE_LOOKBACK_DAYS ... 判定対象とする期間（日数、デフォルト7）

実行方法:
    python scripts/detect_stock_candidates.py
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

from dateutil import tz

from update_data import GEMINI_API_KEY, GEMINI_REQUEST_INTERVAL_SEC, call_gemini_json

ROOT_DIR = Path(__file__).resolve().parent.parent
LIFE_INFO_PATH = ROOT_DIR / "public" / "data" / "life_info.json"
STOCK_SOURCES = [
    ROOT_DIR / "public" / "data" / "child_support_base.json",
    ROOT_DIR / "public" / "data" / "young_adult_support_base.json",
]
REPORT_PATH = ROOT_DIR / "scripts" / ".stock_candidate_report.md"
JST = tz.gettz("Asia/Tokyo")

LOOKBACK_DAYS = int(os.environ.get("STOCK_CANDIDATE_LOOKBACK_DAYS", "7"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("detect_stock_candidates")


# --------------------------------------------------------------------------
# 判定プロンプト
# --------------------------------------------------------------------------
SYSTEM_PROMPT_TEMPLATE = """\
あなたは、熊本県水俣市の非公式生活情報サイト「みなまた くらしナビ」の編集アシスタントです。

このサイトには、時限的な新着記事（フロー）とは別に、恒常的に申請できる支援制度を
常時一覧できる「ストック」ページ（子育て支援制度／一人暮らし・若者支援）があります。
すでにストックに載っている制度の一覧を以下に示します。

【既存のストック制度一覧】
{stock_titles}

これから渡す新着記事1件が、次のどれに該当するか判定してください。

- "existing_update": 既存ストック制度のうち特定の1件について、金額改定・申請期間の
  延長・様式変更など、内容が更新されたことを報じている
- "new_permanent_candidate": 上記一覧に無い、恒常的に申請可能な新しい制度・給付・
  窓口の新設を報じている（期間限定のキャンペーン・単発イベント・募集は含めない）
- "none": 上記どちらでもない（無関係、または期間限定の内容）

迷った場合、特に「一度きりのイベント告知」なのか「継続的に使える制度」なのか
判断がつかない場合は、"none" を選んでください（見逃しより誤検知の方がコストが
高いため、確信が持てる場合のみ報告してください）。

判定結果は以下のJSON形式で**JSONのみ**出力してください。説明文やMarkdownの
コードブロック記号は付けないでください。

{{
  "match_kind": "existing_update" | "new_permanent_candidate" | "none",
  "matched_base_id": "existing_updateの場合、該当するストック項目のbase_id。それ以外はnull",
  "reason": "判定理由を1文で"
}}
"""


# --------------------------------------------------------------------------
# 入出力
# --------------------------------------------------------------------------
def load_life_info_recent() -> list[dict[str, Any]]:
    if not LIFE_INFO_PATH.exists():
        return []
    with LIFE_INFO_PATH.open(encoding="utf-8") as f:
        data = json.load(f)

    cutoff = dt.datetime.now(tz=JST) - dt.timedelta(days=LOOKBACK_DAYS)
    recent: list[dict[str, Any]] = []
    for item in data.get("items", []):
        collected_at = item.get("collected_at")
        if not collected_at:
            continue
        try:
            collected_dt = dt.datetime.fromisoformat(collected_at)
        except ValueError:
            continue
        if collected_dt >= cutoff:
            recent.append(item)
    return recent


def load_stock_titles() -> tuple[str, dict[str, str]]:
    """(プロンプトに埋め込む一覧テキスト, base_id -> title の辞書) を返す。

    permanence=time_limited の項目は monitor_stock.py と同様に対象外とする
    （期間限定の制度は元々ストックの本流ではなく、フロー側で扱う方針のため）。
    """
    lines: list[str] = []
    base_id_to_title: dict[str, str] = {}
    for path in STOCK_SOURCES:
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        for item in data.get("items", []):
            if item.get("permanence") == "time_limited":
                continue
            base_id = item.get("base_id")
            title = item.get("title")
            if not base_id or not title:
                continue
            base_id_to_title[base_id] = title
            lines.append(f"- {title} (`{base_id}`)")
    return "\n".join(lines), base_id_to_title


# --------------------------------------------------------------------------
# 判定
# --------------------------------------------------------------------------
def judge_item(item: dict[str, Any], stock_titles_text: str) -> Optional[dict[str, Any]]:
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(stock_titles=stock_titles_text)
    user_content = (
        f"タイトル: {item.get('title')}\n"
        f"要約: {item.get('summary')}\n"
        f"リンク: {item.get('source_url')}\n"
    )
    return call_gemini_json(system_prompt, user_content, log_label=str(item.get("title")))


def build_report(findings: list[dict[str, Any]]) -> str:
    if not findings:
        return ""

    existing = [f for f in findings if f["match_kind"] == "existing_update"]
    new_candidates = [f for f in findings if f["match_kind"] == "new_permanent_candidate"]

    lines: list[str] = []
    if existing:
        lines.append("\n### 🟡 既存ストック制度の更新情報らしいもの\n")
        for f in existing:
            matched_label = f.get("matched_title") or f.get("matched_base_id") or "（不明）"
            lines.append(
                f"- **{f['item'].get('title')}**\n"
                f"  - 該当しそうな既存項目: {matched_label}\n"
                f"  - 理由: {f['reason']}\n"
                f"  - {f['item'].get('source_url')}"
            )
    if new_candidates:
        lines.append("\n### 🆕 新しい恒常制度の候補らしいもの\n")
        for f in new_candidates:
            lines.append(
                f"- **{f['item'].get('title')}**\n"
                f"  - 理由: {f['reason']}\n"
                f"  - {f['item'].get('source_url')}"
            )
    return "\n".join(lines).strip()


def write_github_output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as f:
        f.write(f"{name}={value}\n")


def main() -> int:
    log.info("=== ストック候補の検出を開始します ===")

    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY が設定されていません。")
        return 1

    stock_titles_text, base_id_to_title = load_stock_titles()
    if not stock_titles_text:
        log.error("既存ストック制度が0件です。child_support_base.json等を確認してください。")
        return 1

    items = load_life_info_recent()
    log.info("直近%d日以内の新着項目: %d件", LOOKBACK_DAYS, len(items))

    if not items:
        log.info("対象項目がありませんでした。終了します。")
        write_github_output("has_findings", "false")
        write_github_output("findings_count", "0")
        return 0

    findings: list[dict[str, Any]] = []
    for i, item in enumerate(items, start=1):
        log.info("[%d/%d] 判定中: %s", i, len(items), item.get("title"))
        result = judge_item(item, stock_titles_text)
        if result and result.get("match_kind") in ("existing_update", "new_permanent_candidate"):
            findings.append(
                {
                    "item": item,
                    "match_kind": result["match_kind"],
                    "matched_base_id": result.get("matched_base_id"),
                    "matched_title": base_id_to_title.get(result.get("matched_base_id") or ""),
                    "reason": result.get("reason") or "",
                }
            )
            log.info("  -> 検知: %s", result["match_kind"])
        time.sleep(GEMINI_REQUEST_INTERVAL_SEC)

    report = build_report(findings)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report, encoding="utf-8")

    has_findings = bool(findings)
    write_github_output("has_findings", "true" if has_findings else "false")
    write_github_output("findings_count", str(len(findings)))

    if has_findings:
        log.info("検知件数: %d件。レポート: %s", len(findings), REPORT_PATH)
    else:
        log.info("検知事項なし。")

    log.info("=== 完了 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
