"""
イベントカレンダー用の分類およびスロット計算モジュール
docs/agent-notes/design-specs/2026-11-11_event-calendar.md の仕様に基づく
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from typing import Any, Dict, Optional, Tuple

CATEGORY_WORDS: dict[str, list[str]] = {
    "market": ["マルシェ", "物産", "漁師市", "朝市", "直売", "即売"],
    "kids": [
        "親子", "子育て", "育児", "ママ", "パパ", "未就学", "乳幼児",
        "マタニティ", "妊婦", "お誕生会", "たまひよ", "こども", "子ども",
        "あそび", "おはなし会",
    ],
    "sports": [
        "スポーツ", "テニス", "野球", "サッカー", "ディスク", "パドル",
        "ボート", "マラソン", "ウォーキング", "歩き方", "ヨガ",
    ],
    "festival": ["祭", "まつり", "フェスタ", "フェスティバル", "サミット", "感謝デー"],
    "culture": ["コンサート", "公演", "ライブ", "演奏", "展", "写真", "書道", "芸能", "合唱", "図書"],
    "learning": ["講座", "教室", "セミナー", "ワークショップ", "体験", "学習会", "勉強会", "作ろう", "会話"],
}

CATEGORY_DISPLAY_NAMES: dict[str, str] = {
    "market": "マルシェ・物産",
    "kids": "子育て・こども",
    "sports": "スポーツ",
    "festival": "お祭り・催し",
    "culture": "文化・芸術",
    "learning": "講座・体験",
    "other": "その他",
}

PREFIX_TAG_PATTERN = re.compile(r"^【([^】]+)】")
TIME_PATTERN = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")
RANGE_SEP_PATTERN = re.compile(r"^\s*[〜~\-－]\s*$")


def classify_category(event: Dict[str, Any]) -> str:
    """イベントのカテゴリを判定する。
    1. タイトル先頭の【タグ】があれば、その内容で判定
    2. タイトル中のキーワードマッチ（表の上から順に優先判定）
    3. 概要（summary）中のキーワードマッチ
    4. いずれもマッチしなければ 'other'
    """
    title = unicodedata.normalize("NFKC", str(event.get("title") or ""))

    # 1. 先頭の【タグ】判定
    m = PREFIX_TAG_PATTERN.match(title)
    if m:
        tag = m.group(1)
        if "マルシェ" in tag:
            return "market"
        if "子育て" in tag or "こども" in tag:
            return "kids"
        if "スポーツ" in tag:
            return "sports"
        if "祭り" in tag or "お祭り" in tag or "催し" in tag:
            return "festival"
        if "文化" in tag or "芸術" in tag:
            return "culture"
        if "講座" in tag or "体験" in tag:
            return "learning"

    # 2. タイトルのキーワード判定（定義順）
    for cat, words in CATEGORY_WORDS.items():
        if any(w in title for w in words):
            return cat

    # 3. 概要のキーワード判定（定義順）
    summary = unicodedata.normalize("NFKC", str(event.get("summary") or ""))
    for cat, words in CATEGORY_WORDS.items():
        if any(w in summary for w in words):
            return cat

    # 4. フォールバック
    return "other"


def parse_times(time_text: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """time_text から開始時刻・終了時刻（HH:MM 形式）を抽出する。
    - 0件: (None, None)
    - 1件: (start, None)
    - 2件: 間が範囲記号かつ「翌」を含まず end > start の場合のみ (start, end)、それ以外は (start, None)
    - 3件以上: (matches[0], None)
    """
    if not time_text:
        return None, None

    norm = unicodedata.normalize("NFKC", str(time_text))
    valid_matches = []
    for m in TIME_PATTERN.finditer(norm):
        h = int(m.group(1))
        minute = int(m.group(2))
        valid_matches.append((m, f"{h:02d}:{minute:02d}"))

    if not valid_matches:
        return None, None

    if len(valid_matches) == 1:
        return valid_matches[0][1], None

    if len(valid_matches) == 2:
        m0, t0 = valid_matches[0]
        m1, t1 = valid_matches[1]
        between = norm[m0.end():m1.start()]
        if (
            RANGE_SEP_PATTERN.fullmatch(between)
            and "翌" not in between
            and t1 > t0
        ):
            return t0, t1
        return t0, None

    # 3件以上
    return valid_matches[0][1], None


def calendar_slot(event: Dict[str, Any]) -> Dict[str, Any]:
    """イベントをカレンダー（.ics・Googleの追加リンク）に入れるときの日時を決める。
    戻り値:
        {
            "all_day": bool,
            "start": str,  # "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM"
            "end": str     # "YYYY-MM-DD" (exclusive) or "YYYY-MM-DDTHH:MM"
        }
    """
    start_date_str = event.get("start_date")
    if not start_date_str:
        raise ValueError("event['start_date'] is required")

    start_d = dt.date.fromisoformat(start_date_str)
    end_date_str = event.get("end_date")
    end_d = dt.date.fromisoformat(end_date_str) if end_date_str else start_d

    day_count = (end_d - start_d).days

    time_text = event.get("time_text")
    start_t, end_t = parse_times(time_text)

    # 1. 単日（day_count == 0）かつ 開始・終了時刻あり
    if day_count == 0 and start_t and end_t:
        return {
            "all_day": False,
            "start": f"{start_date_str}T{start_t}",
            "end": f"{start_date_str}T{end_t}",
        }

    # 2. 単日（day_count == 0）かつ 開始時刻のみ
    if day_count == 0 and start_t and not end_t:
        h, m = map(int, start_t.split(":"))
        if h + 1 >= 24:
            end_t_str = "23:59"
        else:
            end_t_str = f"{h+1:02d}:{m:02d}"
        return {
            "all_day": False,
            "start": f"{start_date_str}T{start_t}",
            "end": f"{start_date_str}T{end_t_str}",
        }

    # 3. 単日（day_count == 0）かつ 時刻なし
    if day_count == 0 and not start_t:
        next_d = start_d + dt.timedelta(days=1)
        return {
            "all_day": True,
            "start": start_date_str,
            "end": next_d.isoformat(),
        }

    # 4. 複数日 14日以内（1 <= day_count <= 13）
    if 1 <= day_count <= 13:
        next_d = end_d + dt.timedelta(days=1)
        return {
            "all_day": True,
            "start": start_date_str,
            "end": next_d.isoformat(),
        }

    # 5. 複数日 15日以上（day_count >= 14）: 初日のみ表示
    next_d = start_d + dt.timedelta(days=1)
    return {
        "all_day": True,
        "start": start_date_str,
        "end": next_d.isoformat(),
    }
