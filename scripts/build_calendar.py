#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
カレンダーファイル生成スクリプト
docs/agent-notes/design-specs/2026-11-11_event-calendar.md の仕様に基づく

入力: public/data/events.json
出力:
  - public/data/events.ics (全件購読用)
  - public/data/ics/<id>.ics (各イベント個別)
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from event_calendar import CATEGORY_DISPLAY_NAMES, calendar_slot, classify_category

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
EVENTS_JSON_PATH = ROOT_DIR / "public" / "data" / "events.json"
EVENTS_ICS_PATH = ROOT_DIR / "public" / "data" / "events.ics"
ICS_DIR = ROOT_DIR / "public" / "data" / "ics"

ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
JST = dt.timezone(dt.timedelta(hours=9))


def escape_ical_text(text: str) -> str:
    """RFC 5545 に従い、バックスラッシュ、セミコロン、カンマ、改行をエスケープする。"""
    if not text:
        return ""
    # バックスラッシュを最初にエスケープ
    t = text.replace("\\", "\\\\")
    t = t.replace(";", "\\;")
    t = t.replace(",", "\\,")
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = t.replace("\n", "\\n")
    return t


def fold_line(line: str) -> str:
    """RFC 5545 に従い、UTF-8バイト長が75バイトを超える行を折り返す。
    折り返された行の先頭には半角スペースを付与する。
    UTF-8のマルチバイト文字の途中では分割しない。
    """
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line

    lines: list[str] = []
    max_len = 75
    while len(encoded) > max_len:
        split_idx = max_len
        # UTF-8 継続バイト (0x80 - 0xBF: 0b10xxxxxx) の途中で切らないよう巻き戻す
        while split_idx > 0 and (encoded[split_idx] & 0xC0) == 0x80:
            split_idx -= 1
        lines.append(encoded[:split_idx].decode("utf-8"))
        encoded = b" " + encoded[split_idx:]
    if encoded:
        lines.append(encoded.decode("utf-8"))
    return "\r\n".join(lines)


def format_dtstamp(last_updated_str: Optional[str]) -> str:
    """events.json の last_updated をUTCの iCalendar 形式 (YYYYMMDDTHHMMSSZ) に変換する。"""
    if not last_updated_str:
        return "19700101T000000Z"
    try:
        d = dt.datetime.fromisoformat(last_updated_str).astimezone(dt.timezone.utc)
        return d.strftime("%Y%m%dT%H%M%SZ")
    except Exception:
        return "19700101T000000Z"


def build_vevent_lines(event: Dict[str, Any], dtstamp: str) -> List[str]:
    """1件のイベントから VEVENT の各プロパティ行リスト（折り返し前）を生成する。"""
    lines = ["BEGIN:VEVENT"]
    ev_id = str(event.get("id") or "")
    lines.append(f"UID:{ev_id}@minamata-support-portal")
    lines.append(f"DTSTAMP:{dtstamp}")

    # 日時とカテゴリは event_calendar.py の関数で毎回計算
    slot = calendar_slot(event)
    category_key = classify_category(event)

    start_d_str = event.get("start_date") or ""
    end_d_str = event.get("end_date") or start_d_str
    start_d = dt.date.fromisoformat(start_d_str)
    end_d = dt.date.fromisoformat(end_d_str)
    day_count = (end_d - start_d).days

    # DTSTART / DTEND
    if slot["all_day"]:
        start_val = slot["start"].replace("-", "")
        end_val = slot["end"].replace("-", "")
        lines.append(f"DTSTART;VALUE=DATE:{start_val}")
        lines.append(f"DTEND;VALUE=DATE:{end_val}")
    else:
        # 時刻あり: 日本時間から9時間引いたUTC (...Z)
        s_dt = dt.datetime.strptime(slot["start"], "%Y-%m-%dT%H:%M").replace(tzinfo=JST).astimezone(dt.timezone.utc)
        e_dt = dt.datetime.strptime(slot["end"], "%Y-%m-%dT%H:%M").replace(tzinfo=JST).astimezone(dt.timezone.utc)
        lines.append(f"DTSTART:{s_dt.strftime('%Y%m%dT%H%M%SZ')}")
        lines.append(f"DTEND:{e_dt.strftime('%Y%m%dT%H%M%SZ')}")

    # SUMMARY: タイトル（15日以上の期間なら（〜M/D）を付ける）
    title = event.get("title") or ""
    if day_count >= 14:
        title = f"{title}（〜{end_d.month}/{end_d.day}）"
    lines.append(f"SUMMARY:{escape_ical_text(title)}")

    # LOCATION: venue (あれば)
    venue = event.get("venue")
    if venue:
        lines.append(f"LOCATION:{escape_ical_text(venue)}")

    # CATEGORIES: カテゴリの表示名 (other のときは書かない)
    if category_key != "other":
        disp = CATEGORY_DISPLAY_NAMES.get(category_key)
        if disp:
            lines.append(f"CATEGORIES:{escape_ical_text(disp)}")

    # URL: source_url (あれば)
    source_url = event.get("source_url")
    if source_url:
        lines.append(f"URL:{source_url}")

    # DESCRIPTION: 改行でつなぐ
    desc_parts: list[str] = []
    summary = event.get("summary")
    if summary:
        desc_parts.append(summary)

    time_text = event.get("time_text")
    if time_text:
        desc_parts.append(f"時間：{time_text}")

    if day_count >= 14:
        desc_parts.append(f"期間：{start_d.month}/{start_d.day}〜{end_d.month}/{end_d.day}（開催日は元のページでご確認ください）")

    # 申し込み
    reg = event.get("registration")
    reg_label = {"required": "要申込", "partial": "一部要申込", "not_required": "申込不要"}.get(reg)
    reg_dl = event.get("registration_deadline")
    reg_note = event.get("registration_note")

    reg_tokens = []
    if reg_label:
        reg_tokens.append(reg_label)
    if reg_dl:
        try:
            dl_d = dt.date.fromisoformat(reg_dl)
            reg_tokens.append(f"{dl_d.month}/{dl_d.day}締切")
        except Exception:
            reg_tokens.append(f"{reg_dl}締切")
    if reg_note:
        reg_tokens.append(reg_note)

    if reg_tokens:
        desc_parts.append("、".join(reg_tokens))

    organizer = event.get("organizer")
    if organizer:
        desc_parts.append(f"主催：{organizer}")

    if source_url:
        desc_parts.append(f"詳細：{source_url}")

    desc_parts.append("みなまた くらしナビ（非公式）掲載。日時・内容は変わることがあります。")

    full_desc = "\n".join(desc_parts)
    lines.append(f"DESCRIPTION:{escape_ical_text(full_desc)}")

    lines.append("END:VEVENT")
    return lines


def build_calendar_ics(events: List[Dict[str, Any]], dtstamp: str, cal_name: Optional[str] = None) -> str:
    """全イベント（または1件）から完全な iCalendar 文字列 (CRLF + fold 済み) を組み立てる。"""
    raw_lines = [
        "BEGIN:VCALENDAR",
        "PRODID:-//minamata-support-portal//events//JA",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:" + escape_ical_text(cal_name or "みなまた くらしナビ イベント（非公式）"),
        "X-WR-TIMEZONE:Asia/Tokyo",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
        "X-PUBLISHED-TTL:PT12H",
    ]

    for ev in events:
        # 日付が壊れた1件のためにカレンダー全体が作れなくならないよう、その1件だけ飛ばす
        try:
            raw_lines.extend(build_vevent_lines(ev, dtstamp))
        except (ValueError, TypeError, KeyError) as err:
            log.warning("カレンダーに入れられないイベントを飛ばしました（%s）: %s", ev.get("id"), err)

    raw_lines.append("END:VCALENDAR")

    folded_lines = [fold_line(line) for line in raw_lines]
    # RFC 5545 改行は CRLF、末尾にも CRLF を付与
    return "\r\n".join(folded_lines) + "\r\n"


def write_if_changed(path: Path, content: str) -> bool:
    """中身が前回と同じファイルは書き換えない。変更があれば書き換えて True を返す。"""
    new_bytes = content.encode("utf-8")
    if path.exists():
        try:
            if path.read_bytes() == new_bytes:
                return False
        except Exception:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(new_bytes)
    return True


def main() -> int:
    log.info("=== カレンダーファイル (.ics) の生成を開始します ===")
    if not EVENTS_JSON_PATH.exists():
        log.error("入力ファイルが見つかりません: %s", EVENTS_JSON_PATH)
        return 1

    with open(EVENTS_JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    events: list[dict] = data.get("items", [])
    dtstamp = format_dtstamp(data.get("last_updated"))
    log.info("イベント件数: %d件, DTSTAMP: %s", len(events), dtstamp)

    # 1. events.ics (購読用全件)
    full_ics_content = build_calendar_ics(events, dtstamp)
    changed = write_if_changed(EVENTS_ICS_PATH, full_ics_content)
    if changed:
        log.info("更新完了: %s", EVENTS_ICS_PATH)
    else:
        log.info("変更なし: %s", EVENTS_ICS_PATH)

    # 1b. events-<キー>.ics (カテゴリ別全7種)
    # 0件のカテゴリでも空のカレンダーとして生成し、購読URLの404を防ぐ
    category_events: dict[str, list[dict]] = {k: [] for k in CATEGORY_DISPLAY_NAMES}
    for ev in events:
        # 判定できないイベントも捨てずに other に入れる（全カテゴリの合計を events.ics と揃えるため）
        try:
            cat = classify_category(ev)
        except Exception as err:  # noqa: BLE001
            log.warning("カテゴリを決められません（%s）: %s", ev.get("id"), err)
            cat = "other"
        category_events[cat if cat in category_events else "other"].append(ev)

    for cat_key, disp_name in CATEGORY_DISPLAY_NAMES.items():
        cat_file = ROOT_DIR / "public" / "data" / f"events-{cat_key}.ics"
        cat_cal_name = f"みなまた くらしナビ イベント：{disp_name}（非公式）"
        cat_ics_content = build_calendar_ics(category_events[cat_key], dtstamp, cal_name=cat_cal_name)
        if write_if_changed(cat_file, cat_ics_content):
            log.info("更新完了: %s (%d件)", cat_file.name, len(category_events[cat_key]))
        else:
            log.info("変更なし: %s (%d件)", cat_file.name, len(category_events[cat_key]))

    # 2. 個別 ics/<id>.ics
    ICS_DIR.mkdir(parents=True, exist_ok=True)
    current_valid_ids = set()
    single_updated_count = 0

    for ev in events:
        ev_id = ev.get("id") or ""
        if not ID_PATTERN.fullmatch(ev_id):
            log.warning("個別カレンダー作成スキップ (不正ID): '%s' - %s", ev_id, ev.get("title"))
            continue

        try:
            build_vevent_lines(ev, dtstamp)
        except (ValueError, TypeError, KeyError):
            continue  # 理由は購読用カレンダーを作るときにログに出している
        current_valid_ids.add(ev_id)
        single_ics_path = ICS_DIR / f"{ev_id}.ics"
        single_ics_content = build_calendar_ics([ev], dtstamp, cal_name=ev.get("title") or "イベント詳細")
        if write_if_changed(single_ics_path, single_ics_content):
            single_updated_count += 1

    log.info("個別 .ics 生成完了: %d件中 %d件書き換え", len(current_valid_ids), single_updated_count)

    # 3. public/data/ics/ の中で今のイベントにない .ics は消す
    removed_count = 0
    for existing_file in ICS_DIR.glob("*.ics"):
        file_id = existing_file.stem
        if file_id not in current_valid_ids:
            try:
                existing_file.unlink()
                removed_count += 1
                log.info("不要な .ics を削除しました: %s", existing_file.name)
            except Exception as e:
                log.error("ファイル削除エラー: %s (%s)", existing_file.name, e)

    log.info("不要ファイル削除: %d件", removed_count)
    log.info("=== カレンダーファイル生成が正常に完了しました ===")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
