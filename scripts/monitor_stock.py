#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
水俣市 くらしナビ ストック情報 監視スクリプト
-------------------------------------------------
child_support_base.json に登録された制度ページ（official_url）を巡回し、
新着情報（フロー）には載らない「サイレント更新」の兆候を検知する。

検知する事象:
    1. リンク切れ（404等）
    2. ページ側の「最終更新日：YYYY年MM月DD日」表示が、JSON側の last_verified
       より新しくなっている（内容が更新された可能性が高い）
    3. 「最終更新日」表示が見つからないページで、本文ハッシュが前回実行時から
       変化している（フォールバック検知。ナビゲーション等の変更にも反応するため
       誤検知が多めに出ることを許容する）

このスクリプトは child_support_base.json を直接書き換えない。
検知結果はレポート（Markdown）として出力するのみで、実際のデータ更新は
人間またはエージェントが公式ページの内容を確認したうえで手動で行う方針とする。
理由: 静的データ（child_support_base.json）は「年1〜数回、手動でメンテナンス」
する方針であり、自動書き換えは誤情報混入のリスクがあるため
（`_schema_note` および decisions-log/2026-08-15_stock-view-no-click.md 参照）。

状態管理:
    本文ハッシュのフォールバック検知のため、前回実行時のハッシュ値を
    public/data/_monitor_state.json に保存する。このファイルはUIから参照されず、
    このスクリプトの実行間でのみ使用する内部状態ファイル。

実行方法:
    python scripts/monitor_stock.py

終了コード:
    0 = 正常終了（検知の有無にかかわらず）。CIでは has_findings 出力で分岐する。
    1 = スクリプト自体の異常（全ページ取得失敗など、設定ミスの疑い）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup
from dateutil import tz

# --------------------------------------------------------------------------
# 基本設定
# --------------------------------------------------------------------------
ROOT_DIR = Path(__file__).resolve().parent.parent
BASE_DATA_PATH = ROOT_DIR / "public" / "data" / "child_support_base.json"
STATE_PATH = ROOT_DIR / "public" / "data" / "_monitor_state.json"
REPORT_PATH = ROOT_DIR / "scripts" / ".monitor_report.md"
JST = tz.gettz("Asia/Tokyo")

REQUEST_TIMEOUT = 20
# HTTPヘッダーはASCII(latin-1)のみ許容されるため、日本語を含めないこと。
USER_AGENT = (
    "MinamataStockMonitorBot/1.0 "
    "(+https://github.com/)"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("monitor_stock")

# 水俣市サイトのCMS（RCMS系）は、記事ページ本文の冒頭付近に
#   <div class="updDate">最終更新日：<time datetime="2024-11-28T16:44:12+09:00">2024年11月28日</time></div>
# という構造化マークアップを出力する（2026-08-23、child-allowanceページ(kiji003155)の
# 実HTMLで確認済み）。datetime属性はISO 8601形式で機械可読なため、これを最優先で使う。
# 万一テンプレートが異なるページに備え、可視テキストからの正規表現抽出をフォールバックとして残す。
LAST_UPDATED_SELECTOR = "time[datetime]"
LAST_UPDATED_TEXT_PATTERN = re.compile(
    r"最終更新日\s*[:：]\s*(\d{4})年(\d{1,2})月(\d{1,2})日"
)

# ページ全体には毎回変わりうるノイズ（アクセス解析用パラメータ等）はほぼ無いが、
# ヘッダー・フッター・ナビゲーションは全ページ共通でありハッシュに含める意味が薄い上、
# サイト側の共通レイアウト変更のたびに全ページが誤検知扱いになってしまう。
# そのため本文ハッシュ計算前にこれらのタグを除去する。
NOISE_TAGS = ["header", "footer", "nav", "script", "style"]


# --------------------------------------------------------------------------
# データ構造
# --------------------------------------------------------------------------
@dataclass
class MonitorTarget:
    base_id: str
    title: str
    official_url: str
    last_verified: Optional[str]


@dataclass
class Finding:
    base_id: str
    title: str
    official_url: str
    kind: str  # "broken_link" | "possibly_updated" | "content_changed_unverified_date" | "fetch_error"
    detail: str


# --------------------------------------------------------------------------
# 入出力
# --------------------------------------------------------------------------
def load_targets() -> list[MonitorTarget]:
    with BASE_DATA_PATH.open(encoding="utf-8") as f:
        data = json.load(f)

    targets: list[MonitorTarget] = []
    for item in data.get("items", []):
        # time_limited（期間限定）の制度は静的データ側から除外する運用のため、
        # 現状はほぼ全件が対象になる想定だが、将来分の混入を避けるため念のため絞り込む。
        if item.get("permanence") == "time_limited":
            continue
        url = item.get("official_url")
        if not url:
            continue
        targets.append(
            MonitorTarget(
                base_id=item["base_id"],
                title=item["title"],
                official_url=url,
                last_verified=item.get("last_verified"),
            )
        )
    return targets


def load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        with STATE_PATH.open(encoding="utf-8") as f:
            return json.load(f)
    return {"items": {}}


def save_state(state: dict[str, Any]) -> None:
    now = dt.datetime.now(tz=JST).isoformat()
    state["last_run"] = now
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = STATE_PATH.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp_path.replace(STATE_PATH)
    log.info("状態を保存しました: %s", STATE_PATH)


# --------------------------------------------------------------------------
# 取得・検知
# --------------------------------------------------------------------------
def http_get(url: str) -> tuple[Optional[requests.Response], Optional[str]]:
    """(response, error_kind) を返す。error_kind は成功時 None。"""
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        log.warning("取得失敗（接続エラー）: %s (%s)", url, e)
        return None, "connection_error"

    if resp.status_code == 404:
        return resp, "not_found"
    if not resp.ok:
        log.warning("取得失敗（HTTP %s）: %s", resp.status_code, url)
        return resp, "http_error"

    # 水俣市サイトはレスポンスヘッダーにcharsetを明示しないため、requestsが
    # HTTP仕様上のデフォルトである ISO-8859-1 に誤判定し、日本語が文字化けする。
    # apparent_encoding（推定）は環境（chardet/charset_normalizerのバージョン差）に
    # よって結果がぶれることが判明したため使わず、生バイトで確認済みの固定値
    # （UTF-8, 先頭にBOMあり）で上書きする（詳細は update_data.py の同名関数を参照）。
    if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = "utf-8-sig"

    return resp, None


def extract_last_updated_from_soup(soup: BeautifulSoup) -> Optional[dt.date]:
    time_tag = soup.select_one(LAST_UPDATED_SELECTOR)
    if time_tag is not None:
        raw = (time_tag.get("datetime") or "").strip()
        if raw:
            try:
                return dt.datetime.fromisoformat(raw).date()
            except ValueError:
                log.warning("time要素のdatetime属性のパースに失敗: %r", raw)

    # フォールバック: 構造化マークアップが見つからない場合、可視テキストから抽出する。
    text = soup.get_text(" ", strip=True)
    m = LAST_UPDATED_TEXT_PATTERN.search(text)
    if not m:
        return None
    y, mo, d = (int(g) for g in m.groups())
    try:
        return dt.date(y, mo, d)
    except ValueError:
        return None


def compute_content_hash(soup: BeautifulSoup) -> str:
    for tag_name in NOISE_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()
    text = soup.get_text(" ", strip=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def check_target(
    target: MonitorTarget, state_items: dict[str, Any]
) -> tuple[Optional[Finding], Optional[dict[str, Any]]]:
    """(finding, new_state_entry) を返す。finding は異常なしなら None。"""
    resp, error_kind = http_get(target.official_url)

    if error_kind == "not_found":
        return (
            Finding(
                target.base_id,
                target.title,
                target.official_url,
                "broken_link",
                "404 Not Found",
            ),
            None,
        )
    if error_kind is not None:
        # 一時的な障害の可能性があるため、リンク切れとは区別して報告する。
        return (
            Finding(
                target.base_id,
                target.title,
                target.official_url,
                "fetch_error",
                f"アクセス失敗（{error_kind}）。サイト側の一時障害の可能性あり。",
            ),
            None,
        )

    assert resp is not None
    soup = BeautifulSoup(resp.text, "html.parser")

    page_date = extract_last_updated_from_soup(soup)
    if page_date is not None:
        state_entry = {"last_page_date": page_date.isoformat()}
        if target.last_verified:
            try:
                verified_date = dt.date.fromisoformat(target.last_verified)
            except ValueError:
                verified_date = None
            if verified_date is not None and page_date > verified_date:
                return (
                    Finding(
                        target.base_id,
                        target.title,
                        target.official_url,
                        "possibly_updated",
                        (
                            f"ページの最終更新日が {page_date.isoformat()} で、"
                            f"データ側の last_verified（{target.last_verified}）より新しい。"
                        ),
                    ),
                    state_entry,
                )
        return None, state_entry

    # 「最終更新日」表示が見つからないページ：本文ハッシュのフォールバック検知。
    content_hash = compute_content_hash(soup)
    state_entry = {"content_hash": content_hash}
    prev_entry = state_items.get(target.base_id)
    prev_hash = prev_entry.get("content_hash") if prev_entry else None

    if prev_hash is not None and prev_hash != content_hash:
        return (
            Finding(
                target.base_id,
                target.title,
                target.official_url,
                "content_changed_unverified_date",
                (
                    "「最終更新日」表示が見つからないページで、本文ハッシュが前回実行時から"
                    "変化。ナビゲーション等の共通部分の変更にも反応するため、誤検知の"
                    "可能性を踏まえて内容を確認してください。"
                ),
            ),
            state_entry,
        )
    return None, state_entry


def build_report(findings: list[Finding]) -> str:
    if not findings:
        return ""

    order = {
        "broken_link": 0,
        "possibly_updated": 1,
        "content_changed_unverified_date": 2,
        "fetch_error": 3,
    }
    findings_sorted = sorted(findings, key=lambda f: order.get(f.kind, 99))

    labels = {
        "broken_link": "🔴 リンク切れ（404）",
        "possibly_updated": "🟡 更新の可能性あり（最終更新日がlast_verifiedより新しい）",
        "content_changed_unverified_date": "⚪ 本文変化を検知（最終更新日表示なし・要目視確認）",
        "fetch_error": "⚠️ 取得エラー（一時障害の可能性）",
    }

    lines: list[str] = []
    current_kind: Optional[str] = None
    for f in findings_sorted:
        if f.kind != current_kind:
            current_kind = f.kind
            lines.append(f"\n### {labels.get(f.kind, f.kind)}\n")
        lines.append(
            f"- **{f.title}** (`{f.base_id}`)\n"
            f"  - {f.detail}\n"
            f"  - {f.official_url}"
        )
    return "\n".join(lines).strip()


def write_github_output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as f:
        f.write(f"{name}={value}\n")


def main() -> int:
    log.info("=== ストック情報の監視を開始します ===")

    targets = load_targets()
    log.info("監視対象: %d 件", len(targets))
    if not targets:
        log.error("監視対象が0件です。child_support_base.json の内容を確認してください。")
        return 1

    state = load_state()
    state_items: dict[str, Any] = state.get("items", {})
    new_state_items: dict[str, Any] = {}

    findings: list[Finding] = []
    fetch_ok = 0
    for i, target in enumerate(targets, start=1):
        log.info("[%d/%d] 確認中: %s", i, len(targets), target.title)
        finding, new_entry = check_target(target, state_items)
        if finding is not None:
            findings.append(finding)
            log.info("  -> 検知: %s (%s)", finding.kind, finding.detail)
        else:
            fetch_ok += 1
        if new_entry is not None:
            new_state_items[target.base_id] = new_entry
        elif target.base_id in state_items:
            # 取得できなかった場合は前回の状態を維持する（ハッシュ比較の連続性を保つ）。
            new_state_items[target.base_id] = state_items[target.base_id]
        time.sleep(1)

    if fetch_ok == 0:
        log.error("すべてのページで正常な取得ができませんでした。ネットワークまたはサイト側の異常の可能性があります。")
        return 1

    state["items"] = new_state_items
    save_state(state)

    report = build_report(findings)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report + "\n", encoding="utf-8")

    has_findings = bool(findings)
    write_github_output("has_findings", "true" if has_findings else "false")
    write_github_output("findings_count", str(len(findings)))

    if has_findings:
        log.info("検知件数: %d 件。レポート: %s", len(findings), REPORT_PATH)
    else:
        log.info("検知事項なし。")

    log.info("=== 完了 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
