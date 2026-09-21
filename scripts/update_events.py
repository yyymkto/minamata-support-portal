#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
水俣市 くらしナビ イベント情報更新スクリプト
-------------------------------------------------
「イベント」タブ用の public/data/events.json を生成する
（設計書: docs/agent-notes/design-specs/2026-09-21_events-tab.md）。

1. 自動抽出: life_info.json のうち topic_tags に「イベント」を含む記事について、
   記事ページ本文をGeminiに読ませ、開催日・時間・場所等を取り出す。
   結果は public/data/_events_auto.json にキャッシュし、同じ記事は再判定しない。
2. 手動登録: Googleスプレッドシート（ウェブに公開したCSV）から読み込む。
   最後に読み込めた内容は public/data/_events_manual.json に残し、
   CSVを取得できなかった日はそれを使う（一時的な失敗で手動登録分が消えないように）。
3. 1と2を統合し、終了済みのイベントを除いて events.json に書き出す。
   スプレッドシートの行の「リンク」が自動抽出イベントのURLと一致する場合は、
   その行で自動抽出分を上書きする（「非表示」列に何か書いてあれば掲載しない）。

update_data.py の後に実行する想定（daily-update.yml）。
life_info.json 自体は書き換えない。

環境変数:
    GEMINI_API_KEY          ... Google AI Studio の Gemini API キー（必須）
    EVENTS_SHEET_CSV_URL    ... 手動登録用スプレッドシートのCSV公開URL（未設定なら手動登録分なし）
    EVENTS_MAX_GEMINI_CALLS ... 1回の実行でGeminiを呼ぶ上限件数（デフォルト40）

実行方法:
    python scripts/update_events.py
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

from bs4 import BeautifulSoup

from update_data import (
    GEMINI_API_KEY,
    GEMINI_CONSECUTIVE_FAILURE_LIMIT,
    GEMINI_REQUEST_INTERVAL_SEC,
    JST,
    call_gemini_json,
    http_get,
    make_id,
)

ROOT_DIR = Path(__file__).resolve().parent.parent
LIFE_INFO_PATH = ROOT_DIR / "public" / "data" / "life_info.json"
EVENTS_PATH = ROOT_DIR / "public" / "data" / "events.json"
AUTO_CACHE_PATH = ROOT_DIR / "public" / "data" / "_events_auto.json"
MANUAL_CACHE_PATH = ROOT_DIR / "public" / "data" / "_events_manual.json"

EVENTS_SHEET_CSV_URL = os.environ.get("EVENTS_SHEET_CSV_URL", "").strip()

EVENT_TOPIC_TAG = "イベント"
MAX_GEMINI_CALLS = int(os.environ.get("EVENTS_MAX_GEMINI_CALLS", "40"))
# go-minamata.jp の記事は本文の前にナビゲーションメニューの文字列が約600字入るため、
# 本文が切れないよう余裕を持たせる（2026-09-21、実HTMLで確認）。
ARTICLE_TEXT_MAX_CHARS = 6000

log = logging.getLogger("update_events")

EVENT_EXTRACTION_SYSTEM_PROMPT = """\
あなたは熊本県水俣市の地域情報サイトの編集者です。
渡された記事（タイトルと本文）が「一般の人が参加・観覧・来場できる催し」の告知かどうかを判定し、
催しであれば開催情報を取り出してください。

出力は次のJSONのみ:
{
  "is_event": true または false,
  "start_date": "YYYY-MM-DD" または null,
  "end_date": "YYYY-MM-DD" または null,
  "time_text": "開始・終了時刻（例: 10:00〜15:00）" または null,
  "venue": "会場名" または null,
  "summary": "どんな催しかを120字以内で。日付・会場の繰り返しは不要" または null,
  "organizer": "主催者名" または null
}

is_event を true にするもの:
- 水俣市内（または水俣市が関わる近隣の会場）で開かれ、一般の人が参加・観覧・来場できる催し
  （祭り、マルシェ、花火、スポーツ大会・教室、講演会、体験会、展示など）

判定・抽出の対象は「タイトルが示す催し」です:
- 本文の中で別の催しに触れていても、その催しの日付・会場は返さないでください。
  （例: タイトルが「来年の世界大会の開催決定」で、本文で今年の全国大会にも触れている場合は、世界大会の日程を返す）
- 複数の催しを一覧でまとめて紹介する記事（「〇〇対象イベントについて」「イベント案内」等）は is_event を false にしてください。
  ただし、1つの企画名のもとで期間中に複数の会場・店舗で行われるもの（例: まちゼミ、はしご酒イベント）は
  1つの催しとして扱い、企画全体の期間を返してください。

is_event を false にするもの:
- 出店者・出演者・参加団体・ボランティア・デザイン案などの「募集」の告知
  （催しそのものの告知ではないため）
- 開催結果の報告、テレビ・雑誌で紹介されたことのお知らせ
- 投票の呼びかけ、キャンペーン、スタンプラリーなど、特定の開催日・会場を持たないもの
- 施設の開館状況、災害関連のお知らせ

日付のルール:
- 年が省略されている場合は、本文の文脈と「今日の日付」から補ってください。
- 雨天時の順延日・予備日は end_date に含めないでください。
- 毎月・毎週など繰り返し開かれる催しは、本文に書かれている直近の開催日1回分を返してください。
- 1日だけの催しは end_date を start_date と同じにしてください。
- 本文から開催日を特定できない場合は start_date を null にしてください（推測で埋めないこと）。
- 「最終更新日」「掲載日」は開催日ではありません。
"""

DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 市サイトのカテゴリ一覧ページ（例: /list00321.html）は個別の記事ではないため判定しない
# （2026-09-21、試し実行で一覧ページが10/3のイベントとして拾われたため追加）。
LIST_PAGE_PATTERN = re.compile(r"/list\d+\.html$")


# --------------------------------------------------------------------------
# 入出力
# --------------------------------------------------------------------------
def load_json(path: Path, default: Any) -> Any:
    if path.exists():
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


# --------------------------------------------------------------------------
# 自動抽出
# --------------------------------------------------------------------------
def fetch_article_text(url: str) -> Optional[str]:
    resp = http_get(url)
    if resp is None:
        return None
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    body = soup.body or soup
    return body.get_text(" ", strip=True)[:ARTICLE_TEXT_MAX_CHARS]


def parse_iso_date(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not DATE_PATTERN.match(value.strip()):
        return None
    try:
        return dt.date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return None


def clean_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def source_label_for(source_name: str) -> str:
    if source_name.startswith("みなまた観光物産協会"):
        return "みなまた観光物産協会"
    return "水俣市"


def pick_result(result: Any, today: dt.date) -> Optional[dict]:
    """Gemini応答から判定結果を1件取り出す。

    指示に反して [ {...}, {...} ] のように配列で返してくることがある
    （2026-09-21、試し実行で発生。まちゼミなど1記事に複数の開催日がある場合）。
    その場合は、まだ終わっていないもののうち開始日が最も早いものを採用し、
    無ければ先頭を採用する。
    """
    if isinstance(result, dict):
        return result
    if not isinstance(result, list):
        return None
    dicts = [r for r in result if isinstance(r, dict)]
    if not dicts:
        return None
    today_iso = today.isoformat()
    upcoming = [
        r for r in dicts
        if parse_iso_date(r.get("start_date"))
        and (parse_iso_date(r.get("end_date")) or parse_iso_date(r.get("start_date"))) >= today_iso
    ]
    if upcoming:
        return min(upcoming, key=lambda r: parse_iso_date(r.get("start_date")))
    return dicts[0]


def extract_event(item: dict, today: dt.date) -> tuple[Optional[dict], str]:
    """(キャッシュに記録するエントリ or None, 結果) を返す。

    結果は "answered"（判定できた）/ "fetch_failed"（記事ページを取得できなかった）/
    "gemini_failed"（Geminiから回答を得られなかった）のいずれか。
    取得失敗・Gemini呼び出し失敗は一時的な可能性があるため、
    キャッシュに記録せず次回また判定する（update_data.py の _checked_urls.json と同じ方針）。
    """
    url = item["source_url"]
    text = fetch_article_text(url)
    if text is None:
        return None, "fetch_failed"

    user_content = (
        f"今日の日付: {today.isoformat()}\n"
        f"タイトル: {item['title']}\n"
        f"本文:\n{text}\n"
    )
    result = call_gemini_json(
        EVENT_EXTRACTION_SYSTEM_PROMPT, user_content, log_label=item["title"]
    )
    raw = result
    result = pick_result(raw, today)
    if result is None:
        if raw is not None:
            log.warning("Gemini応答の形式が想定外です: %s", str(raw)[:200])
        return None, "gemini_failed"

    now = dt.datetime.now(tz=JST).isoformat()
    entry: dict[str, Any] = {"is_event": bool(result.get("is_event")), "checked_at": now, "event": None}
    start_date = parse_iso_date(result.get("start_date"))
    if not entry["is_event"] or start_date is None:
        return entry, "answered"

    end_date = parse_iso_date(result.get("end_date")) or start_date
    if end_date < start_date:
        end_date = start_date

    entry["event"] = {
        "id": make_id(url),
        "title": item["title"],
        "start_date": start_date,
        "end_date": end_date,
        "time_text": clean_text(result.get("time_text")),
        "venue": clean_text(result.get("venue")),
        "summary": clean_text(result.get("summary")),
        "organizer": clean_text(result.get("organizer")),
        "source_url": url,
        "source_label": source_label_for(item.get("source_name") or ""),
        "origin": "auto",
    }
    return entry, "answered"


def update_auto_cache(life_items: list[dict], today: dt.date) -> dict[str, dict]:
    cache: dict[str, dict] = load_json(AUTO_CACHE_PATH, {"entries": {}}).get("entries", {})

    # life_info.json から消えた記事は、もう判定対象にならないのでキャッシュからも消す
    live_urls = {i["source_url"] for i in life_items}
    cache = {url: e for url, e in cache.items() if url in live_urls}

    targets = [
        i for i in life_items
        if EVENT_TOPIC_TAG in (i.get("topic_tags") or [])
        and not LIST_PAGE_PATTERN.search(i["source_url"])
        and i["source_url"] not in cache
    ]
    log.info("イベント判定の対象（未判定）: %d 件", len(targets))
    if len(targets) > MAX_GEMINI_CALLS:
        log.info("1回あたりの上限%d件を超えるため、残り%d件は次回に回します。",
                 MAX_GEMINI_CALLS, len(targets) - MAX_GEMINI_CALLS)
        targets = targets[:MAX_GEMINI_CALLS]

    consecutive_failures = 0
    for n, item in enumerate(targets, start=1):
        log.info("[%d/%d] イベント判定中: %s", n, len(targets), item["title"])
        entry, outcome = extract_event(item, today)
        if outcome == "answered":
            cache[item["source_url"]] = entry
            consecutive_failures = 0
        elif outcome == "gemini_failed":
            # 記事ページの取得失敗（削除済みページの404等）はGeminiの障害とは無関係なので数えない
            consecutive_failures += 1
            if consecutive_failures >= GEMINI_CONSECUTIVE_FAILURE_LIMIT:
                log.warning("判定が%d件連続で失敗したため、残り%d件の判定を打ち切ります。",
                            consecutive_failures, len(targets) - n)
                break
        time.sleep(GEMINI_REQUEST_INTERVAL_SEC)

    write_json(AUTO_CACHE_PATH, {"entries": dict(sorted(cache.items()))})
    return cache


# --------------------------------------------------------------------------
# 統合・出力
# --------------------------------------------------------------------------
TITLE_NOISE_PATTERN = re.compile(r"[\s　「」『』【】（）()！!？?・、。～〜\-－]|開催(します|されます|のお知らせ)?")


def normalize_title(title: str) -> str:
    return TITLE_NOISE_PATTERN.sub("", title)


def filled_count(event: dict) -> int:
    return sum(1 for k in ("time_text", "venue", "summary", "organizer") if event.get(k))


def dedupe_events(events: list[dict]) -> list[dict]:
    """同じイベントを1件にまとめる。

    次の2つを「同じイベント」とみなす（2026-09-21、試し実行で確認）:
    - 市サイトが同じ記事を /kiji0034822/ と /sports/kiji0034822/ のように複数URLで掲載している
    - 同じ催しを市サイトと観光物産協会サイトの両方が告知している
      （例:「湯の児花火あそび　開催されます！」と「湯の児 花火あそび」）
    判定条件は、開始日・終了日が同じで、空白・記号・「開催」等を除いたタイトルの
    一方がもう一方に含まれること。残すのは項目が多く埋まっている方、同数ならURLが短い方。
    """
    ranked = sorted(
        events, key=lambda e: (-filled_count(e), len(e.get("source_url") or ""))
    )
    kept: list[dict] = []
    for e in ranked:
        norm = normalize_title(e["title"])
        is_dup = any(
            k["start_date"] == e["start_date"]
            and k["end_date"] == e["end_date"]
            and (norm in normalize_title(k["title"]) or normalize_title(k["title"]) in norm)
            for k in kept
        )
        if not is_dup:
            kept.append(e)
    return kept


# --------------------------------------------------------------------------
# 手動登録（Googleスプレッドシート）
# --------------------------------------------------------------------------
# スプレッドシートの日付セルは、表示形式によって「2026/10/03」「2026-10-03」
# 「2026年10月3日」などの文字列でCSVに出力されるため、いずれも受け付ける。
SHEET_DATE_PATTERN = re.compile(r"^(\d{4})\s*[/\-.年]\s*(\d{1,2})\s*[/\-.月]\s*(\d{1,2})\s*日?")
REQUIRED_SHEET_COLUMNS = ("イベント名", "開始日")


def parse_sheet_date(value: str) -> Optional[str]:
    m = SHEET_DATE_PATTERN.match(value.strip())
    if not m:
        return None
    try:
        return dt.date(*(int(g) for g in m.groups())).isoformat()
    except ValueError:
        return None


def clean_link(link: str) -> Optional[str]:
    """Instagram等のリンクから共有時に付く追跡用パラメータを取り除く。

    Instagramの「リンクをコピー」で得たURLには ?utm_source=...&stkn=... が付き、
    stkn は共有した人を識別するトークンとみられるため、サイトに載せない
    （2026-09-22、実際の登録行で確認）。投稿の特定には ?以降は不要。
    市サイト等のURLはクエリが意味を持つことがあるため、Instagramに限定する。
    """
    link = link.strip()
    if not link:
        return None
    if "instagram.com" in link:
        link = link.split("?", 1)[0].split("#", 1)[0]
    return link


def default_source_label(link: Optional[str]) -> str:
    if link and "instagram.com" in link:
        return "Instagram"
    return "詳細"


def parse_sheet_csv(text: str) -> Optional[dict]:
    """CSV本文を {"events": [...], "hidden_urls": [...], "warnings": [...]} に変換する。

    見出し行に必須列が無い場合（シートが公開されておらずログイン画面のHTMLが返ってきた等）は None。
    """
    reader = csv.DictReader(io.StringIO(text))
    headers = [(h or "").strip() for h in (reader.fieldnames or [])]
    if not all(col in headers for col in REQUIRED_SHEET_COLUMNS):
        log.error("スプレッドシートの見出し行に %s がありません（見出し: %s）。",
                  "・".join(REQUIRED_SHEET_COLUMNS), headers[:10])
        return None

    events: list[dict] = []
    hidden_urls: list[str] = []
    warnings: list[str] = []
    # 1行目が見出しなので、データはスプレッドシート上の2行目から始まる
    for row_no, raw in enumerate(reader, start=2):
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items() if k is not None}
        if not any(row.values()):
            continue
        title = row.get("イベント名", "")
        link = clean_link(row.get("リンク", ""))

        if row.get("非表示"):
            if link:
                hidden_urls.append(link)
            continue

        start_date = parse_sheet_date(row.get("開始日", ""))
        if not title or start_date is None:
            msg = f"{row_no}行目: イベント名または開始日が空か、開始日を日付として読めません（{title or '名前なし'} / {row.get('開始日', '')}）"
            log.warning("スプレッドシート %s", msg)
            warnings.append(msg)
            continue

        end_date = start_date
        if row.get("終了日"):
            parsed_end = parse_sheet_date(row["終了日"])
            if parsed_end is None:
                msg = f"{row_no}行目: 終了日を日付として読めないため、開始日と同じ日として扱いました（{title} / {row['終了日']}）"
                log.warning("スプレッドシート %s", msg)
                warnings.append(msg)
            elif parsed_end >= start_date:
                end_date = parsed_end

        digest = hashlib.sha1(f"{title}|{start_date}|{link or ''}".encode("utf-8")).hexdigest()[:10]
        events.append({
            "id": f"m-{digest}",
            "title": title,
            "start_date": start_date,
            "end_date": end_date,
            "time_text": row.get("時間") or None,
            "venue": row.get("場所") or None,
            "summary": row.get("概要") or None,
            "organizer": row.get("主催") or None,
            "source_url": link,
            "source_label": row.get("情報元") or None,  # 空欄の既定値は統合時に決める
            "origin": "manual",
        })

    return {"events": events, "hidden_urls": hidden_urls, "warnings": warnings}


def load_manual() -> dict:
    """手動登録分を読み込む。取得できなかった場合は前回読み込めた内容を使う。"""
    empty = {"events": [], "hidden_urls": [], "warnings": []}
    if not EVENTS_SHEET_CSV_URL:
        log.info("EVENTS_SHEET_CSV_URL が未設定のため、手動登録分はありません。")
        return empty

    previous = load_json(MANUAL_CACHE_PATH, empty)
    resp = http_get(EVENTS_SHEET_CSV_URL)
    parsed = None
    if resp is not None:
        # GoogleのCSVはUTF-8。BOMが付いていても見出し名がずれないよう utf-8-sig で読む
        parsed = parse_sheet_csv(resp.content.decode("utf-8-sig", errors="replace"))
    if parsed is None:
        log.warning("スプレッドシートを読み込めなかったため、前回読み込めた手動登録分（%d件）を使います。",
                    len(previous.get("events", [])))
        return previous

    log.info("スプレッドシートから読み込み: 掲載 %d 件 / 非表示指定 %d 件 / 読めなかった行 %d 件",
             len(parsed["events"]), len(parsed["hidden_urls"]), len(parsed["warnings"]))
    write_json(MANUAL_CACHE_PATH, {"fetched_at": dt.datetime.now(tz=JST).isoformat(), **parsed})
    return parsed


# --------------------------------------------------------------------------
# 統合・出力
# --------------------------------------------------------------------------
def merge_manual(auto_events: list[dict], manual: dict) -> list[dict]:
    """自動抽出分にスプレッドシートの上書き・非表示を適用し、手動登録分を足す。"""
    hidden = set(manual.get("hidden_urls", []))
    auto_by_url = {e["source_url"]: e for e in auto_events}

    manual_events = []
    for m in manual.get("events", []):
        m = dict(m)
        if m.get("source_url") in hidden:
            continue
        if not m.get("source_label"):
            # 自動抽出分の上書き行なら、情報元の表示は元のまま（例: みなまた観光物産協会）にする
            overridden = auto_by_url.get(m.get("source_url"))
            m["source_label"] = overridden["source_label"] if overridden else default_source_label(m.get("source_url"))
        manual_events.append(m)

    replaced = hidden | {m["source_url"] for m in manual_events if m.get("source_url")}
    return [e for e in auto_events if e["source_url"] not in replaced] + manual_events


def build_events(cache: dict[str, dict], manual: dict, today: dt.date) -> list[dict]:
    events = dedupe_events([e["event"] for e in cache.values() if e.get("event")])
    events = merge_manual(events, manual)
    today_iso = today.isoformat()
    events = [e for e in events if e["end_date"] >= today_iso]
    events.sort(key=lambda e: (e["start_date"], e["title"]))
    return events


def save_events(events: list[dict]) -> None:
    previous = load_json(EVENTS_PATH, {}).get("items")
    if previous == events:
        log.info("イベント一覧に変更はありません（%d件）。events.json は書き換えません。", len(events))
        return
    write_json(EVENTS_PATH, {
        "last_updated": dt.datetime.now(tz=JST).isoformat(),
        "generated_by": "scripts/update_events.py",
        "items": events,
    })
    log.info("保存完了: %s (%d件)", EVENTS_PATH, len(events))


def main() -> int:
    log.info("=== イベント情報の更新を開始します ===")
    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY が設定されていません。")
        return 1

    today = dt.datetime.now(tz=JST).date()
    life_items = load_json(LIFE_INFO_PATH, {"items": []}).get("items", [])
    cache = update_auto_cache(life_items, today)
    manual = load_manual()
    save_events(build_events(cache, manual, today))
    log.info("=== 完了 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
