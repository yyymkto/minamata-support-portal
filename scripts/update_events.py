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
   申し込み情報（要否・締切・方法）もここで取り出す。申し込み情報が入る前の形式のキャッシュは、
   開催前のものだけ1回判定し直す（設計書: docs/agent-notes/design-specs/2026-10-07_event-registration.md）。
2. 手動登録: Googleスプレッドシート（ウェブに公開したCSV）から読み込む。
   最後に読み込めた内容は public/data/_events_manual.json に残し、
   CSVを取得できなかった日はそれを使う（一時的な失敗で手動登録分が消えないように）。
3. 投稿フォーム: 主催者がGoogleフォームで申し込み、吉野さんが承認した分を、
   スプレッドシートの「掲載用」タブ（ウェブに公開したCSV）から読み込む
   （設計書: docs/agent-notes/design-specs/2026-09-28_event-submission-form.md）。
   最後に読み込めた内容は public/data/_events_form.json に残す。
4. 1〜3を統合し、終了済みのイベントを除いて events.json に書き出す。
   優先順位は 自動抽出 < 投稿フォーム < 手動登録。リンクが同じなら優先度の高い方で置き換え、
   手動登録の「非表示」列に何か書いてある行のリンクは、どの経路のイベントでも掲載しない。
5. シートを読み込めなかったときは、GitHub Actions の出力 sheet_error=true を書き出す
   （ワークフローがIssueを起票する）。終了コードは0のまま。

update_data.py の後に実行する想定（daily-update.yml）。
life_info.json 自体は書き換えない。

環境変数:
    GEMINI_API_KEY          ... Google AI Studio の Gemini API キー（必須）
    EVENTS_SHEET_CSV_URL    ... 手動登録用スプレッドシートのCSV公開URL（未設定なら手動登録分なし）
    EVENTS_FORM_CSV_URL     ... 投稿フォームの「掲載用」タブのCSV公開URL（未設定なら投稿分なし）
    GITHUB_OUTPUT           ... GitHub Actions が設定する。シートの読み込み失敗を書き出す先
    EVENTS_MAX_GEMINI_CALLS ... 1回の実行でGeminiを呼ぶ上限件数（デフォルト40）

実行方法:
    python scripts/update_events.py
"""

from __future__ import annotations

import csv
import difflib
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

import calendar
import unicodedata
from urllib.parse import urljoin

from bs4 import BeautifulSoup
import pypdf

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
FORM_CACHE_PATH = ROOT_DIR / "public" / "data" / "_events_form.json"
KOHO_CACHE_PATH = ROOT_DIR / "public" / "data" / "_events_koho.json"

KOHO_LIST_URL = "https://www.city.minamata.lg.jp/list00422.html"
KOHO_PAGE_MIN_CHARS = 200

EVENTS_SHEET_CSV_URL = os.environ.get("EVENTS_SHEET_CSV_URL", "").strip()
EVENTS_FORM_CSV_URL = os.environ.get("EVENTS_FORM_CSV_URL", "").strip()

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
  "organizer": "主催者名" または null,
  "registration": "required" または "partial" または "not_required" または null,
  "registration_deadline": "YYYY-MM-DD" または null,
  "registration_note": "申込方法と条件を50字以内で" または null
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

申し込みのルール（対象は「タイトルが示す催し」。本文に書いてあることだけを返し、推測で埋めないこと）:
- registration:
  - "required": 参加・入場・観覧のために、事前の申し込み・予約・チケット購入・整理券の事前入手が必要
  - "partial": 催しの中の一部の企画だけが事前申し込み制で、ほかは申し込みなしで参加・来場できる
    （例:「申し込み不要（〇〇教室を除く）」）
  - "not_required": 事前の申し込みが要らないことを示す言葉が本文にそのまま書いてあるときだけ
    （「申込不要」「申し込みは不要」「予約不要」「自由参加」「直接会場へお越しください」「当日会場で受付」など）
  - null: 上のどれにも当たらないとき。次の言葉だけでは "not_required" にしないこと:
    「どなたでも」「お気軽に」「入場無料」「観覧無料」「ご来場ください」「ご参加をお待ちしております」
    （申し込みが要らないとは書いていないため）。祭り・マルシェ・展示だからといって推測しないこと
  - 当日会場で行う抽選・整理券配布・先着販売（例: 限定〇食の当日抽選）は事前の申し込みではありません
- registration_deadline: 「申込締切」「〆切」「〜日まで」「申込期間 〜〇日」など、申し込みの期限として書かれた日付だけ。
  受付開始日・開催日・掲載日は入れない。「定員になり次第締切」だけで日付が無ければ null。
  当日券・当日受付もある場合は、前売りや事前申込の期限があっても null。
  registration が "required" か "partial" でなければ null。
- registration_note: 申込方法（QRコード・電話・FAX・メール・窓口・各店舗など）と、定員・先着・抽選の別、当日券の有無を50字以内で。
  本文に書いてあることだけを短くまとめ、本文に無い言葉を足さないこと。電話番号は本文に書いてあるものだけ。URLは入れない。
  registration が null なら null。"not_required" なら当日の受付方法など補足があるときだけ。
"""

KOHO_EVENT_EXTRACTION_SYSTEM_PROMPT = """\
あなたは熊本県水俣市の地域情報サイトの編集者です。
市の広報紙「広報みなまた」の紙面テキストから、一般の人が参加・観覧・来場できる催しの開催情報を取り出してください。

出力は次のJSONのみ:
{
  "events": [
    {
      "title": "催しの名前（簡潔に）",
      "start_date": "YYYY-MM-DD",
      "end_date": "YYYY-MM-DD" または null,
      "time_text": "開始・終了時刻（例: 10:00〜15:00）" または null,
      "venue": "会場名" または null,
      "summary": "どんな催しかを120字以内で。日付・会場の繰り返しは不要" または null,
      "organizer": "主催者名（団体名のみ）" または null,
      "registration": "required" または "partial" または "not_required" または null,
      "registration_deadline": "YYYY-MM-DD" または null,
      "registration_note": "申込方法と条件を50字以内で" または null
    }
  ]
}

対象:
- 水俣市内で開かれ、一般の人が参加・観覧・来場できる催し（祭り、展示、公演、講座、教室、体験会、交流会、地区の文化祭など）

対象外（events に含めない）:
- 水俣市外の会場の催し（芦北町・津奈木町など）
- 訪問先・見学先が水俣市外の見学・研修・視察・バスツアーなど（集合場所が市内であっても対象外）
- 業者・事業者・団体向けの説明会、現地見学会、企画提案募集、入札関連（指定管理者の現地見学会など）
- 個別相談会、出張相談会、常設の相談窓口（タイトルが「相談会」「相談所」で終わるものなど）
- 手続きや証明書発行の窓口の開設（休日窓口、マイナンバーカード窓口など）
- 開催済みの報告（フォトリポート、まちのわだい）
- 参加者・出店者・ボランティアなどの募集だけの告知、市の手続きのお知らせ

1件の単位:
- 名前の違う催しは別々の1件にしてください（例: こどもセンターの①〜⑦の講座は7件）。
- 催しの中の個別のブースや企画・体験コーナー（例: 健康まつり内の測定ブースなど）は別の1件にせず、催し全体で1件にしてください。
- 同じ名前の連続講座は1件にし、start_date は初回、end_date は最終回とし、各回の日付は summary に書いてください。

日付のルール:
- 年が省略されている場合は、発行年月から補ってください（例: 11月号に書かれた 3/13 は翌年）。
- 雨天時の順延日・予備日は end_date に含めないでください。
- 1日だけの催しは end_date を start_date と同じにしてください。
- 別々の日に開く相談会や催し（例:「11/10、11/24」など）を期間（start_dateからend_dateまで）として扱わないでください（相談会はそもそも対象外ですが、複数回開催を連続期間にしないこと）。
- 本文から開催日を特定できないものは除外してください。

カレンダー形式のページ（「くらしのカレンダー」等、日付が「日 曜日 行事」で並んでいるページ）での特別なルール:
- 日付: 日付は各行の「日の数字＋曜日」から読み取ってください（例:「21 土」なら発行年月の21日）。
- 同じ名前の催しが複数の日にある場合（例: 英語会話テーブルなど）: 期間としてまとめず、日ごとに別の1件として出力してください（各回の start_date / end_date をその日にし、その日の時刻・会場を出力する。カレンダーでは summary に各回の日付を書けないため）。
- 開始日: 1日の行に「【～】」が付く催しだけ出力しないでください（前月以前から継続開催されており開始日を特定できないため）。2日以降の行は「【～】」が付いていても、その行の日を開始日にして出力してください（例: 6日の行の「総合展示【〜11/8】」は開始日を6日として出力する）。
- 説明文と申し込み: カレンダーには名前・時刻・会場しか書かれていないため、summary と registration は必ず null にしてください（名前から内容や申込方法を推測して創作しないこと）。

個人情報のルール:
- 個人の氏名と携帯電話番号は絶対に出力しないでください。
- organizer は団体名だけにし、個人の名前や、団体名が分からなければ null にしてください。

申し込みのルール（本文に書いてあることだけを返し、推測で埋めないこと）:
- registration:
  - "required": 参加・入場・観覧のために事前の申し込み・予約・チケット購入・整理券の事前入手が必要と本文に明記されているとき
  - チケット購入が必要な公演・コンサート・催しは registration を "required" にし、チケット料金・入場料を summary に書いてください。
  - 参加者募集の告知や定員の記載があることだけを理由に推測で "required" にしないでください。事前の申し込みが必要である旨が本文に明記されている場合のみ "required" にしてください。
  - "partial": 催しの中の一部の企画だけが事前申し込み制で、ほかは自由に参加できる
  - "not_required": 事前の申し込みが要らないことが本文にそのまま書いてあるときだけ（「申込不要」「自由参加」「当日受付」など）
  - null: 上のどれにも当てはまらない、または記載がないとき。推測で "not_required" にしないこと
  - 「当日参加も可能」「当日参加可」と書かれているものは "partial" にしないでください（registration は null にしてください）。
- registration_deadline: 申し込みの期限として書かれた日付だけ。無ければ null。registration が "required" か "partial" でなければ null。
- registration_note: 申込方法（QRコード・電話・メール・窓口など）と定員・先着・抽選の別を50字以内で。個人の氏名や携帯電話番号は含めないこと。URLは入れない。
  - 本文に書かれていない申込方法（「電話にて」など）を推測で補わないこと。
"""


REGISTRATION_VALUES = ("required", "partial", "not_required")
# 自動抽出キャッシュの形式。2で申し込み情報（registration*）を追加した（2026-10-07、設計書 event-registration）。
# これより古いエントリのうち開催前のイベントは、申し込み情報を取り出すために1回だけ判定し直す。
CACHE_SCHEMA = 2
REGISTRATION_NOTE_MAX_CHARS = 60
URL_IN_TEXT_PATTERN = re.compile(r"https?://\S+")
# AIの判定を本文の言葉で裏づける（2026-10-07、試し実行で「どなた様もお気軽に」だけの記事に
# 「当日直接会場へ」と本文に無い文を作って「申込不要」にした例があったため）。
# 「申込不要」は、申し込みが要らないと書いてある言葉が本文にあるときだけ残す。
NOT_REQUIRED_EVIDENCE = re.compile(
    r"(申し?込み?|予約|事前登録)(は|の必要は)?(不要|必要(あり|ござい)?ません)|参加自由|自由参加|入退場自由|"
    r"直接(会場|お越し)|当日(会場で|、?会場にて|に会場で)?受付|"
    r"(会場|現地|テント|本部)[^。]{0,10}で受付"  # 例:「ふるさと広場内の大型テントで受付します」
)
# 「要申込」「一部要申込」は、申し込みに関わる言葉が本文に1つも無ければ捨てる
# （当日会場での抽選・受付だけの催しを「一部要申込」にした例があったため）。
REQUIRED_EVIDENCE = re.compile(r"申し?込|予約|チケット|前売|整理券|応募|エントリー|受付終了")

DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 市サイトのカテゴリ一覧ページ（例: /list00321.html）は個別の記事ではないため判定しない
# （2026-09-21、試し実行で一覧ページが10/3のイベントとして拾われたため追加）。
LIST_PAGE_PATTERN = re.compile(r"/list\d+\.html$")

KOHO_TITLE_PATTERN = re.compile(
    r"広報みなまた\s*令和\s*(\d+|元)\s*年\s*(\d+)(?:[・/／~〜\-\s]+\d+)?\s*月(?:号|合併号)?"
)
MOBILE_PHONE_PATTERN = re.compile(r"0[789]0-?\d{4}-?\d{4}")



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


def normalize_registration(registration: Any, deadline: Any, note: Any,
                           end_date: Optional[str]) -> dict[str, Optional[str]]:
    """申し込み情報の3項目を、表示してよい組み合わせに整える。

    - 締切は「要申込」「一部要申込」のときだけ持つ。終了日より後の締切は読み違いとみなして捨てる。
    - 申込方法は、要否が不明（null）なら持たない。URLは取り除く（元ページから申し込んでもらうため）。
    """
    reg = registration if registration in REGISTRATION_VALUES else None
    dl = parse_iso_date(deadline) if reg in ("required", "partial") else None
    if dl and end_date and dl > end_date:
        dl = None
    text = clean_text(note) if reg else None
    if text:
        text = clean_text(URL_IN_TEXT_PATTERN.sub("", text).strip(" 　、。:："))
    if text and len(text) > REGISTRATION_NOTE_MAX_CHARS:
        text = text[:REGISTRATION_NOTE_MAX_CHARS - 1] + "…"
    return {"registration": reg, "registration_deadline": dl, "registration_note": text}


def registration_from_article(result: dict, article_text: str, end_date: Optional[str]) -> dict[str, Optional[str]]:
    """Geminiの判定結果から申し込み情報を作る。本文に裏づけの言葉が無い判定は null にする。"""
    reg = result.get("registration")
    if reg == "not_required" and not NOT_REQUIRED_EVIDENCE.search(article_text):
        reg = None
    elif reg in ("required", "partial") and not REQUIRED_EVIDENCE.search(article_text):
        reg = None
    return normalize_registration(reg, result.get("registration_deadline"),
                                  result.get("registration_note"), end_date)


def with_registration_defaults(event: dict) -> dict:
    """古い形式のイベント（申し込み項目が無い）にも3項目を null で持たせる。"""
    return {"registration": None, "registration_deadline": None, "registration_note": None, **event}


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


def extract_event(item: dict, today: dt.date) -> tuple[Optional[dict], str, dict]:
    """(キャッシュに記録するエントリ or None, 結果, 申し込み情報) を返す。

    結果は "answered"（判定できた）/ "fetch_failed"（記事ページを取得できなかった）/
    "gemini_failed"（Geminiから回答を得られなかった）のいずれか。
    取得失敗・Gemini呼び出し失敗は一時的な可能性があるため、
    キャッシュに記録せず次回また判定する（update_data.py の _checked_urls.json と同じ方針）。

    申し込み情報は、イベントではないと判定された場合も返す（判定し直しで使う。refresh_registration 参照）。
    """
    no_registration = normalize_registration(None, None, None, None)
    url = item["source_url"]
    text = fetch_article_text(url)
    if text is None:
        return None, "fetch_failed", no_registration

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
        return None, "gemini_failed", no_registration

    now = dt.datetime.now(tz=JST).isoformat()
    entry: dict[str, Any] = {"is_event": bool(result.get("is_event")), "checked_at": now,
                             "schema": CACHE_SCHEMA, "event": None}
    start_date = parse_iso_date(result.get("start_date"))
    end_date = parse_iso_date(result.get("end_date")) or start_date
    if start_date and end_date < start_date:
        end_date = start_date
    registration = registration_from_article(result, text, end_date)
    if not entry["is_event"] or start_date is None:
        return entry, "answered", registration

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
        **registration,
    }
    return entry, "answered", registration


def needs_registration_refresh(entry: dict, today_iso: str) -> bool:
    """申し込み情報が無い古い形式のエントリのうち、まだ終わっていないイベントか。"""
    event = entry.get("event")
    return (entry.get("schema", 1) < CACHE_SCHEMA
            and bool(event)
            and (event.get("end_date") or "") >= today_iso)


def refresh_registration(old: dict, registration: dict) -> dict:
    """古いエントリに、判定し直した結果の申し込み情報だけを足す。

    開催日・会場などは掲載済みの内容のまま変えない（判定し直しでイベントが消えたり日付が動いたりしないように）。
    判定し直しで「イベントではない」「開催日が分からない」と揺れた場合も、申し込み情報は使い、掲載は続ける
    （2026-10-07、試し実行でまちゼミの判定が揺れたため）。締切は掲載済みの終了日で確かめ直す。
    """
    event = old["event"]
    registration = normalize_registration(
        registration["registration"], registration["registration_deadline"],
        registration["registration_note"], event.get("end_date"))
    return {**old, "schema": CACHE_SCHEMA, "event": {**event, **registration}}


def update_auto_cache(life_items: list[dict], today: dt.date) -> dict[str, dict]:
    cache: dict[str, dict] = load_json(AUTO_CACHE_PATH, {"entries": {}}).get("entries", {})

    # life_info.json から消えた記事は、もう判定対象にならないのでキャッシュからも消す
    live_urls = {i["source_url"] for i in life_items}
    cache = {url: e for url, e in cache.items() if url in live_urls}

    new_targets = [
        i for i in life_items
        if EVENT_TOPIC_TAG in (i.get("topic_tags") or [])
        and not LIST_PAGE_PATTERN.search(i["source_url"])
        and i["source_url"] not in cache
    ]
    # 新しい記事の掲載が遅れないよう、申し込み情報のための判定し直しは未判定の記事の後に回す
    refresh_targets = [
        i for i in life_items
        if i["source_url"] in cache and needs_registration_refresh(cache[i["source_url"]], today.isoformat())
    ]
    log.info("イベント判定の対象: 未判定 %d 件 / 申し込み情報の判定し直し %d 件",
             len(new_targets), len(refresh_targets))
    targets = new_targets + refresh_targets
    if len(targets) > MAX_GEMINI_CALLS:
        log.info("1回あたりの上限%d件を超えるため、残り%d件は次回に回します。",
                 MAX_GEMINI_CALLS, len(targets) - MAX_GEMINI_CALLS)
        targets = targets[:MAX_GEMINI_CALLS]

    consecutive_failures = 0
    for n, item in enumerate(targets, start=1):
        url = item["source_url"]
        is_refresh = url in cache
        log.info("[%d/%d] %s: %s", n, len(targets),
                 "申し込み情報を判定し直し中" if is_refresh else "イベント判定中", item["title"])
        entry, outcome, registration = extract_event(item, today)
        if outcome == "answered":
            cache[url] = refresh_registration(cache[url], registration) if is_refresh else entry
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
    nfkc = unicodedata.normalize("NFKC", title)
    return TITLE_NOISE_PATTERN.sub("", nfkc)


def filled_count(event: dict) -> int:
    return sum(1 for k in ("time_text", "venue", "summary", "organizer", "registration_note") if event.get(k))


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


# --------------------------------------------------------------------------
# 広報みなまた自動抽出（設計書: docs/agent-notes/design-specs/2026-11-02_koho-events.md）
# --------------------------------------------------------------------------
def parse_koho_title(text: str) -> Optional[tuple[int, int]]:
    """「広報みなまた令和N年M月号」から (西暦年, 最初の手の発行月) を返す。
    全角数字や合併号（7・8月号等）にも対応する。
    """
    normalized = unicodedata.normalize("NFKC", text).strip()
    m = KOHO_TITLE_PATTERN.search(normalized)
    if not m:
        return None
    era_str, month_str = m.groups()
    era_year = 1 if era_str == "元" else int(era_str)
    year = 2018 + era_year
    month = int(month_str)
    if not (1 <= month <= 12):
        return None
    return year, month


def fetch_koho_issues(list_url: str = KOHO_LIST_URL) -> list[dict[str, str]]:
    """広報みなまた一覧ページから号一覧を取得する。新しい号が先頭になる。"""
    resp = http_get(list_url)
    if resp is None:
        log.warning("広報一覧ページの取得に失敗しました: %s", list_url)
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    issues: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for a in soup.find_all("a"):
        text = a.get_text(strip=True)
        parsed = parse_koho_title(text)
        if not parsed:
            continue
        href = a.get("href")
        if not href:
            continue
        url = urljoin(list_url, href)
        if url in seen_urls:
            continue
        if LIST_PAGE_PATTERN.search(url):
            continue
        seen_urls.add(url)
        year, month = parsed
        issue_month = f"{year:04d}-{month:02d}"
        clean_title = unicodedata.normalize("NFKC", text).strip()
        issues.append({
            "url": url,
            "title": clean_title,
            "issue_month": issue_month,
        })
    issues.sort(key=lambda x: x["issue_month"], reverse=True)
    return issues


def fetch_koho_pdf_url(kiji_url: str) -> Optional[str]:
    """号の記事ページからPDFのリンクを取得する。"""
    resp = http_get(kiji_url)
    if resp is None:
        log.warning("広報記事ページの取得に失敗しました: %s", kiji_url)
        return None
    soup = BeautifulSoup(resp.text, "html.parser")
    for a in soup.find_all("a"):
        href = a.get("href", "")
        if href.lower().endswith(".pdf"):
            return urljoin(kiji_url, href)
    log.warning("広報記事ページ内にPDFリンクが見つかりませんでした: %s", kiji_url)
    return None


def extract_pages_from_pdf(pdf_url: str) -> list[tuple[int, str]]:
    """PDFをダウンロードし、各ページの (ページ番号(1始まり), 抽出テキスト) のリストを返す。"""
    resp = http_get(pdf_url)
    if resp is None or not resp.content:
        log.warning("広報PDFの取得に失敗しました: %s", pdf_url)
        return []
    reader = pypdf.PdfReader(io.BytesIO(resp.content))
    pages: list[tuple[int, str]] = []
    for idx, page in enumerate(reader.pages, 1):
        txt = page.extract_text() or ""
        pages.append((idx, txt))
    return pages


def sanitize_mobile_phones(text: Optional[str]) -> Optional[str]:
    """個人の携帯電話番号パターンを取り除く。"""
    if not text:
        return None
    cleaned = MOBILE_PHONE_PATTERN.sub("", text).strip(" 　、。:：,/")
    return cleaned or None


def is_date_supported_in_text(
    date_str: str,
    page_text: str,
    issue_month: Optional[str] = None,
) -> bool:
    r"""ページ本文に前後に数字が続かない M/D または M月D日 が含まれているか確認する。
    「くらしのカレンダー」を含むページでは、(?<!\d)日の数字+曜日[^\d■]{0,8}■ の形式で確認する。
    """
    try:
        d = dt.date.fromisoformat(date_str)
    except ValueError:
        return False

    # 丸数字（①〜⑳等）は直後の日付（11月等）と連結して「111月」とならないよう分離記号に置換
    cleaned_text = re.sub(r"[①-⑳㉑-㉟㊱-㊿㈠-㈩\u2460-\u2473\u24b6-\u24ea]", "■", page_text)
    norm_text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", cleaned_text))

    # 前後に数字が続かない M/D または M月D日
    p1 = rf"(?<!\d){d.month}/{d.day}(?!\d)"
    p2 = rf"(?<!\d){d.month}月{d.day}日(?!\d)"
    if re.search(p1, norm_text) or re.search(p2, norm_text):
        return True

    # カレンダーの扱い: 「くらしのカレンダー」を含むページに限定
    if "くらしのカレンダー" in norm_text and issue_month:
        try:
            iy, im = int(issue_month[:4]), int(issue_month[5:7])
            if d.year == iy and d.month == im:
                weekday_char = ["月", "火", "水", "木", "金", "土", "日"][d.weekday()]
                p_cal = rf"(?<!\d){d.day}{weekday_char}[^\d■]{{0,8}}■"
                if re.search(p_cal, norm_text):
                    return True
        except Exception:
            pass

    return False


def is_date_in_koho_range(date_str: str, issue_month: str) -> bool:
    """start_date が発行月の前月1日より前、または発行月の12か月後より後でないか確認する。"""
    try:
        d = dt.date.fromisoformat(date_str)
        y, m = int(issue_month[:4]), int(issue_month[5:7])
        prev_y = y if m > 1 else y - 1
        prev_m = m - 1 if m > 1 else 12
        min_date = dt.date(prev_y, prev_m, 1)
        max_y = y + 1
        max_m = m
        max_d = calendar.monthrange(max_y, max_m)[1]
        max_date = dt.date(max_y, max_m, max_d)
        return min_date <= d <= max_date
    except Exception:
        return False


def extract_start_time(time_text: Optional[str]) -> Optional[str]:
    """time_text から最初の H:MM を抽出する。"""
    if not time_text:
        return None
    m = re.search(r"(\d{1,2}:\d{2})", unicodedata.normalize("NFKC", time_text))
    return m.group(1) if m else None


def dedupe_koho_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """広報どうしの重複を除く。
    開始日が同じで、次のいずれかを満たす場合、同じイベントとみなす：
    1. タイトルの類似度が0.8以上（difflib）
    2. normalize_title() 後のタイトルの一方がもう一方に含まれる
    3. 会場と開始時刻が同じ（会場は normalize_title() で正規化して比較、開始時刻は最初の H:MM を比較）
    項目が多く埋まっているほうを残す。同数ならURLが短いほう。
    """
    ranked = sorted(
        events, key=lambda e: (-filled_count(e), len(e.get("source_url") or ""))
    )
    kept: list[dict[str, Any]] = []
    for e in ranked:
        t_norm = normalize_title(e["title"])
        v_e = normalize_title(e.get("venue") or "")
        st_e = extract_start_time(e.get("time_text"))
        is_dup = False
        for k in kept:
            if k.get("start_date") == e.get("start_date"):
                k_norm = normalize_title(k["title"])
                ratio = difflib.SequenceMatcher(None, t_norm, k_norm).ratio()
                is_sub = (t_norm in k_norm) or (k_norm in t_norm)
                v_k = normalize_title(k.get("venue") or "")
                st_k = extract_start_time(k.get("time_text"))
                same_venue_time = bool(v_e and v_k and v_e == v_k and st_e and st_k and st_e == st_k)

                if ratio >= 0.8 or is_sub or same_venue_time:
                    is_dup = True
                    reason = ("類似度>=0.8" if ratio >= 0.8 else
                              ("タイトル包含" if is_sub else "会場・開始時刻一致"))
                    log.info("広報重複統合（%s）: '%s' を残し '%s' を除外",
                             reason, k["title"], e["title"])
                    break
        if not is_dup:
            kept.append(e)
    kept.sort(key=lambda e: (e.get("start_date") or "", e.get("title") or ""))
    return kept


def validate_koho_event(
    raw: dict[str, Any],
    page_text: str,
    page_num: int,
    issue_month: str,
    pdf_url: str,
) -> Optional[dict[str, Any]]:
    """Geminiの抽出結果1件をプログラム側で検査・整形する。不適格なら None を返す。"""
    title = clean_text(raw.get("title"))
    if not title:
        return None
    title = sanitize_mobile_phones(title)
    if not title:
        return None

    # タイトルが「相談会」「相談所」で終わるものを除外
    clean_title = re.sub(r"[」』）\)\]】\s　]+$", "", title)
    if clean_title.endswith("相談会") or clean_title.endswith("相談所"):
        log.info("広報イベント除外（相談会・相談所）: %s", title)
        return None

    # 手続きや証明書発行の窓口を除外
    if "窓口" in title or "休日窓口" in title:
        log.info("広報イベント除外（窓口開設）: %s", title)
        return None

    # 業者・事業者・団体向け説明会・見学会を除外
    if ("見学会" in title or "説明会" in title) and ("指定管理者" in title or "事業者" in title or "業者" in title):
        log.info("広報イベント除外（事業者向け説明会・見学会）: %s", title)
        return None

    # 市外訪問・見学を除外
    if any(w in title for w in ("合志", "菊池", "芦北", "津奈木", "出水", "八代", "熊本市")):
        if "見学" in title or "訪問" in title or "研修" in title:
            log.info("広報イベント除外（市外訪問・見学）: %s", title)
            return None

    start_date = parse_iso_date(raw.get("start_date"))
    if not start_date:
        log.info("広報イベント除外（開始日なし）: %s", title)
        return None

    # 日付の範囲
    if not is_date_in_koho_range(start_date, issue_month):
        log.info("広報イベント除外（日付範囲外 %s for %s）: %s", start_date, issue_month, title)
        return None

    # 日付の裏づけ
    if not is_date_supported_in_text(start_date, page_text, issue_month=issue_month):
        log.info("広報イベント除外（紙面に日付記載なし %s）: %s", start_date, title)
        return None

    venue = clean_text(raw.get("venue"))
    venue = sanitize_mobile_phones(venue)
    # 市外（芦北町・津奈木町・合志市・出水市など）
    if venue and any(w in venue for w in ("合志", "菊池", "芦北", "津奈木", "出水", "八代", "熊本市")):
        log.info("広報イベント除外（市外会場 %s）: %s", venue, title)
        return None

    # カレンダーの扱い（カレンダーページなら summary / registration を null にする）
    norm_page = re.sub(r"\s+", "", unicodedata.normalize("NFKC", page_text))
    is_calendar_page = "くらしのカレンダー" in norm_page

    # カレンダー月初（1日）で【〜11/15】等が付く前月からの継続催しは除外
    if is_calendar_page:
        try:
            d = dt.date.fromisoformat(start_date)
            if d.day == 1:
                norm_t = re.sub(r"\s+", "", unicodedata.normalize("NFKC", clean_title))
                prefix = re.escape(norm_t[:min(len(norm_t), 6)])
                if re.search(rf"{prefix}[^【]{{0,15}}【[~～]", norm_page):
                    log.info("広報イベント除外（カレンダー月初継続イベント %s）: %s", start_date, title)
                    return None
        except Exception:
            pass

    end_date = parse_iso_date(raw.get("end_date")) or start_date
    if end_date < start_date:
        end_date = start_date

    # 申し込み情報の確認
    if is_calendar_page:
        registration = {"registration": None, "registration_deadline": None, "registration_note": None}
    else:
        registration = registration_from_article(raw, page_text, end_date)
        reg_note = sanitize_mobile_phones(registration.get("registration_note"))
        registration["registration_note"] = reg_note

    time_text = sanitize_mobile_phones(clean_text(raw.get("time_text")))
    summary = None if is_calendar_page else sanitize_mobile_phones(clean_text(raw.get("summary")))
    organizer = sanitize_mobile_phones(clean_text(raw.get("organizer")))

    month_num = int(issue_month.split("-")[1])
    event_hash = hashlib.sha1(f"{title}|{start_date}".encode("utf-8")).hexdigest()[:10]
    source_url = f"{pdf_url}#page={page_num}&event={event_hash}"
    source_label = f"広報みなまた{month_num}月号（PDF {page_num}ページ）"

    return {
        "id": make_id(source_url),
        "title": title,
        "start_date": start_date,
        "end_date": end_date,
        "time_text": time_text,
        "venue": venue,
        "summary": summary,
        "organizer": organizer,
        "source_url": source_url,
        "source_label": source_label,
        "origin": "koho",
        **registration,
    }


def extract_events_from_koho_page(
    page_text: str,
    page_num: int,
    issue_title: str,
    issue_month: str,
    pdf_url: str,
    today: dt.date,
    *,
    timeout: int = 30,
) -> tuple[list[dict[str, Any]], bool]:
    """1ページの紙面テキストからGeminiでイベントを抽出し、(イベント一覧, 成功したか) を返す。"""
    user_content = (
        f"今日の日付: {today.isoformat()}\n"
        f"広報紙の号: {issue_title}\n"
        f"発行年月: {issue_month}\n"
        f"PDFページ番号: {page_num}\n"
        f"紙面テキスト:\n{page_text}\n"
    )
    result = call_gemini_json(
        KOHO_EVENT_EXTRACTION_SYSTEM_PROMPT,
        user_content,
        log_label=f"{issue_title} p.{page_num}",
        timeout=timeout,
    )
    if result is None:
        return [], False

    raw_events = result.get("events")
    if not isinstance(raw_events, list):
        log.warning("Gemini応答に events リストがありません: %s", str(result)[:200])
        return [], False

    valid_events: list[dict[str, Any]] = []
    for raw in raw_events:
        if isinstance(raw, dict):
            ev = validate_koho_event(raw, page_text, page_num, issue_month, pdf_url)
            if ev:
                valid_events.append(ev)

    return valid_events, True


def load_koho_cache() -> dict[str, Any]:
    return load_json(KOHO_CACHE_PATH, {"last_list_ok_date": None, "issues": {}})


def save_koho_cache(cache: dict[str, Any]) -> None:
    write_json(KOHO_CACHE_PATH, cache)


def is_koho_issue_done(issue: dict[str, Any]) -> bool:
    """号の全ページを処理済みか。PDFを読めていない号（total_pages なし）は未完了とする。"""
    total_pages = issue.get("total_pages")
    done = set(issue.get("pages_done", [])) | set(issue.get("pages_skipped", []))
    return bool(total_pages) and len(done) >= total_pages


def update_koho_cache(
    today: dt.date,
    cache: Optional[dict[str, Any]] = None,
    max_calls: int = MAX_GEMINI_CALLS,
) -> tuple[dict[str, Any], int, bool]:
    """広報みなまたからイベントを抽出しキャッシュを更新する。
    (更新後キャッシュ, Gemini呼び出し回数, 一覧取得成功フラグ) を返す。
    """
    if cache is None:
        cache = load_koho_cache()

    issues = fetch_koho_issues(KOHO_LIST_URL)
    list_ok = len(issues) > 0
    if list_ok:
        cache["last_list_ok_date"] = today.isoformat()
        # 新しい号がない日もファイルに残す。残さないと、次に一覧取得が1回失敗しただけで
        # 古い日付から3日以上たったと判定され、誤って通知が出る。
        save_koho_cache(cache)
    else:
        log.warning("広報一覧から号を取得できませんでした。")

    if not issues:
        return cache, 0, list_ok

    # 初回（issues が空）は最新号のみを対象とし、過去号は遡らない
    existing_issues = cache.get("issues", {})
    if not existing_issues:
        target_issues = [issues[0]]
        log.info("初回実行: 最新号 %s のみを対象にします。", target_issues[0]["title"])
    else:
        # 記録済みの最新号より新しい号と、全ページが完了していない号を対象とする。
        # 過去の号はさかのぼらず、完了した号はPDFを取得し直さない。
        latest_month = max(e.get("issue_month") or "" for e in existing_issues.values())
        target_issues = []
        for iss in issues:
            entry = existing_issues.get(iss["url"])
            if entry is None:
                if iss["issue_month"] > latest_month:
                    target_issues.append(iss)
            elif not is_koho_issue_done(entry):
                target_issues.append(iss)

    calls = 0
    consecutive_failures = 0

    for iss in target_issues:
        if calls >= max_calls or consecutive_failures >= GEMINI_CONSECUTIVE_FAILURE_LIMIT:
            break

        url = iss["url"]
        entry = cache.setdefault("issues", {}).setdefault(url, {
            "title": iss["title"],
            "issue_month": iss["issue_month"],
            "pdf_url": "",
            "pages_done": [],
            "pages_skipped": [],
            "events": [],
        })

        pdf_url = entry.get("pdf_url")
        if not pdf_url:
            pdf_url = fetch_koho_pdf_url(url)
            if not pdf_url:
                continue
            entry["pdf_url"] = pdf_url
            save_koho_cache(cache)

        pages = extract_pages_from_pdf(pdf_url)
        if not pages:
            continue
        entry["total_pages"] = len(pages)

        pages_done = set(entry.get("pages_done", []))
        pages_skipped = set(entry.get("pages_skipped", []))

        for page_num, page_text in pages:
            if calls >= max_calls or consecutive_failures >= GEMINI_CONSECUTIVE_FAILURE_LIMIT:
                break
            if page_num in pages_done or page_num in pages_skipped:
                continue

            # 200字未満はスキップ
            if len(page_text.strip()) < KOHO_PAGE_MIN_CHARS:
                log.info("%s p.%d は文字数不足(%d字)のためスキップします。", iss["title"], page_num, len(page_text.strip()))
                entry["pages_skipped"].append(page_num)
                entry["pages_skipped"].sort()
                save_koho_cache(cache)
                continue

            # Gemini抽出
            log.info("%s p.%d (%d字) のイベント抽出を実行します...", iss["title"], page_num, len(page_text))
            start_t = time.time()
            events, ok = extract_events_from_koho_page(
                page_text=page_text,
                page_num=page_num,
                issue_title=iss["title"],
                issue_month=iss["issue_month"],
                pdf_url=pdf_url,
                today=today,
            )
            elapsed = time.time() - start_t
            calls += 1

            if ok:
                consecutive_failures = 0
                entry["pages_done"].append(page_num)
                entry["pages_done"].sort()
                # 既存イベントから同じ source_url のものを差し替え/追加し、広報内重複を除去
                existing_evs = [e for e in entry.get("events", [])
                                if not any(ne["source_url"] == e.get("source_url") for ne in events)]
                entry["events"] = dedupe_koho_events(existing_evs + events)
                log.info("%s p.%d 完了: %d件抽出 (所要時間: %.2f秒)", iss["title"], page_num, len(events), elapsed)
                save_koho_cache(cache)
                time.sleep(GEMINI_REQUEST_INTERVAL_SEC)
            else:
                consecutive_failures += 1
                log.warning("%s p.%d 失敗 (連続失敗: %d/%d, 所要時間: %.2f秒)",
                            iss["title"], page_num, consecutive_failures, GEMINI_CONSECUTIVE_FAILURE_LIMIT, elapsed)
                if consecutive_failures >= GEMINI_CONSECUTIVE_FAILURE_LIMIT:
                    log.warning("Gemini API の連続失敗が上限(%d)に達したため、本日の広報抽出を中断します。",
                                GEMINI_CONSECUTIVE_FAILURE_LIMIT)
                    break

    return cache, calls, list_ok

# 手動登録（Googleスプレッドシート）
# --------------------------------------------------------------------------
# スプレッドシートの日付セルは、表示形式によって「2026/10/03」「2026-10-03」
# 「2026年10月3日」などの文字列でCSVに出力されるため、いずれも受け付ける。
SHEET_DATE_PATTERN = re.compile(r"^(\d{4})\s*[/\-.年]\s*(\d{1,2})\s*[/\-.月]\s*(\d{1,2})\s*日?")
REQUIRED_SHEET_COLUMNS = ("イベント名", "開始日")
# スプレッドシートの数式エラー。投稿フォームの「掲載用」タブは数式で作るため、フォームの質問タイトルや
# 「承認」の見出しが変わると #N/A 等になる。1行の読み取りエラーとして流すとフォーム分が黙って
# 全部消えるので、シートごと読み込み失敗として扱う（2026-09-28、設計書 event-submission-form）。
SHEET_ERROR_VALUE_PATTERN = re.compile(r"^#(N/A|REF!|VALUE!|NAME\?|ERROR!|DIV/0!|NUM!|NULL!)")
# 「申込」列の値（2026-10-07、設計書 event-registration）。列自体が無いシートも読める（任意列）。
SHEET_REGISTRATION_VALUES = {"必要": "required", "一部必要": "partial", "不要": "not_required"}


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


def parse_sheet_csv(text: str, origin: str = "manual", id_prefix: str = "m") -> Optional[dict]:
    """CSV本文を {"events": [...], "hidden_urls": [...], "warnings": [...]} に変換する。

    見出し行に必須列が無い場合（シートが公開されておらずログイン画面のHTMLが返ってきた等）と、
    数式エラーのセルがある場合は None。
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
        error_cells = [f"{k}={v}" for k, v in row.items() if SHEET_ERROR_VALUE_PATTERN.match(v)]
        if error_cells:
            log.error("スプレッドシートの%d行目に数式エラーがあります（%s）。", row_no, ", ".join(error_cells))
            return None
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

        registration = None
        reg_text = row.get("申込", "")
        if reg_text:
            registration = SHEET_REGISTRATION_VALUES.get(reg_text)
            if registration is None:
                msg = f"{row_no}行目: 申込は「必要」「一部必要」「不要」のどれかで書いてください。空欄として扱いました（{title} / {reg_text}）"
                log.warning("スプレッドシート %s", msg)
                warnings.append(msg)
        deadline = None
        if row.get("申込締切"):
            deadline = parse_sheet_date(row["申込締切"])
            if deadline is None:
                msg = f"{row_no}行目: 申込締切を日付として読めないため、空欄として扱いました（{title} / {row['申込締切']}）"
                log.warning("スプレッドシート %s", msg)
                warnings.append(msg)
            elif deadline > end_date:
                msg = f"{row_no}行目: 申込締切がイベントの終了日より後のため、空欄として扱いました（{title} / {row['申込締切']}）"
                log.warning("スプレッドシート %s", msg)
                warnings.append(msg)
                deadline = None
        # 手で書いた申込方法は文字数で切らない（フォームは50字までに制限している）
        note = row.get("申込方法") or None
        reg_fields = {
            "registration": registration,
            "registration_deadline": deadline if registration in ("required", "partial") else None,
            "registration_note": note if registration else None,
        }

        digest = hashlib.sha1(f"{title}|{start_date}|{link or ''}".encode("utf-8")).hexdigest()[:10]
        events.append({
            "id": f"{id_prefix}-{digest}",
            "title": title,
            "start_date": start_date,
            "end_date": end_date,
            "time_text": row.get("時間") or None,
            "venue": row.get("場所") or None,
            "summary": row.get("概要") or None,
            "organizer": row.get("主催") or None,
            "source_url": link,
            "source_label": row.get("情報元") or None,  # 空欄の既定値は統合時に決める
            "origin": origin,
            **reg_fields,  # 「申込」が空欄の上書き行は、統合時に置き換え先の値を引き継ぐ
        })

    return {"events": events, "hidden_urls": hidden_urls, "warnings": warnings}


def load_sheet(url: str, cache_path: Path, name: str, origin: str, id_prefix: str,
               failures: list[str]) -> dict:
    """公開CSVのシートを読み込む。取得できなかった場合は前回読み込めた内容を使い、failures に理由を足す。

    手動登録用のシート1と、投稿フォームの「掲載用」タブの両方に使う。
    """
    empty = {"events": [], "hidden_urls": [], "warnings": []}
    if not url:
        log.info("%sのURLが未設定のため、この分のイベントはありません。", name)
        return empty

    previous = load_json(cache_path, empty)
    resp = http_get(url)
    parsed = None
    if resp is None:
        failures.append(f"{name}: CSVを取得できませんでした")
    else:
        # GoogleのCSVはUTF-8。BOMが付いていても見出し名がずれないよう utf-8-sig で読む
        parsed = parse_sheet_csv(resp.content.decode("utf-8-sig", errors="replace"), origin, id_prefix)
        if parsed is None:
            failures.append(f"{name}: 見出し行に「イベント名」「開始日」が無いか、#N/A などのエラーが入ったセルがあります")
    if parsed is None:
        log.warning("%sを読み込めなかったため、前回読み込めた内容（%d件）を使います。",
                    name, len(previous.get("events", [])))
        return previous

    log.info("%sから読み込み: 掲載 %d 件 / 非表示指定 %d 件 / 読めなかった行 %d 件",
             name, len(parsed["events"]), len(parsed["hidden_urls"]), len(parsed["warnings"]))
    write_json(cache_path, {"fetched_at": dt.datetime.now(tz=JST).isoformat(), **parsed})
    return parsed


def report_sheet_failures(failures: list[str]) -> None:
    """シートの読み込み失敗を GitHub Actions のステップ出力に書き出す（ワークフローがIssueを起票する）。"""
    if not failures:
        return
    for f in failures:
        log.warning("シートの読み込み失敗: %s", f)
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as f:
        f.write("sheet_error=true\n")
        f.write(f"sheet_error_detail={' / '.join(failures)}\n")


# --------------------------------------------------------------------------
# 統合・出力
# --------------------------------------------------------------------------
def _fill_source_label(event: dict, base_by_url: dict[str, dict]) -> dict:
    """情報元が空欄の行に表示名を決める。
    置き換える先のイベントがあれば、情報元の表示は元のまま（例: みなまた観光物産協会）にする。

    「申込」が空欄の行も、置き換える先のイベントがあれば、その申し込み情報を引き継ぐ
    （申込の列ができる前に作った上書き行で、自動抽出した申し込み情報が消えないように。2026-10-07）。
    """
    event = dict(event)
    url = event.get("source_url")
    overridden = base_by_url.get(url) if url else None
    if not event.get("source_label"):
        event["source_label"] = overridden["source_label"] if overridden else default_source_label(url)
    if not event.get("registration") and overridden:
        base = with_registration_defaults(overridden)
        for key in ("registration", "registration_deadline", "registration_note"):
            event[key] = base[key]
    return event


def merge_form(auto_events: list[dict], form: dict) -> list[dict]:
    """自動抽出分に投稿フォーム分を足す。リンクが同じ自動抽出分は、主催者の情報で置き換える。"""
    auto_by_url = {e["source_url"]: e for e in auto_events if e.get("source_url")}
    form_events = [_fill_source_label(f, auto_by_url) for f in form.get("events", [])]
    replaced = {f["source_url"] for f in form_events if f.get("source_url")}
    return [e for e in auto_events if e["source_url"] not in replaced] + form_events


def merge_manual(base_events: list[dict], manual: dict) -> list[dict]:
    """自動抽出分・投稿フォーム分にスプレッドシート（シート1）の上書き・非表示を適用し、手動登録分を足す。"""
    hidden = set(manual.get("hidden_urls", []))
    base_by_url = {e["source_url"]: e for e in base_events if e.get("source_url")}

    manual_events = [_fill_source_label(m, base_by_url) for m in manual.get("events", [])
                     if m.get("source_url") not in hidden]

    replaced = hidden | {m["source_url"] for m in manual_events if m.get("source_url")}
    return [e for e in base_events if not e.get("source_url") or e["source_url"] not in replaced] + manual_events


def get_koho_events(koho_cache: dict) -> list[dict]:
    """広報キャッシュから全号のイベントを収集し、各イベントに issue_month を付与して返す。"""
    events = []
    for issue in koho_cache.get("issues", {}).values():
        issue_month = issue.get("issue_month")
        for ev in issue.get("events", []):
            item = dict(ev)
            item["issue_month"] = issue_month
            events.append(item)
    return events


def drop_koho_duplicates(events: list[dict]) -> list[dict]:
    """広報イベントの重複を除去する。
    開始日が同じで、normalize_title() 後のタイトルの一方がもう一方に含まれるイベントを同じものとみなす。
    - 広報以外の経路と重なれば、広報分を捨てる（記事ページのほうが軽く、詳しく開けるため）。
    - 広報どうしで重なれば、新しい号を残す（号が同じなら情報量が多い方を残す）。
    """
    non_koho = [e for e in events if e.get("origin") != "koho"]
    koho = [e for e in events if e.get("origin") == "koho"]

    # 1. 広報以外の経路と重なる広報分を捨てる
    filtered_koho = []
    for k in koho:
        k_norm = normalize_title(k["title"])
        k_sd = k.get("start_date")
        overlaps_non_koho = any(
            nk.get("start_date") == k_sd
            and (k_norm in normalize_title(nk["title"]) or normalize_title(nk["title"]) in k_norm)
            for nk in non_koho
        )
        if overlaps_non_koho:
            log.info("広報重複除外（一般記事優先）: '%s' を除外", k["title"])
            continue
        filtered_koho.append(k)

    # 2. 広報どうしで重なる場合、新しい号を残す（号が同じなら filled_count が多い方）
    sorted_koho = sorted(
        filtered_koho,
        key=lambda e: (
            e.get("issue_month") or "",
            filled_count(e),
            -len(e.get("source_url") or "")
        ),
        reverse=True
    )
    kept_koho: list[dict] = []
    for k in sorted_koho:
        k_norm = normalize_title(k["title"])
        k_sd = k.get("start_date")
        is_dup = any(
            kk.get("start_date") == k_sd
            and (k_norm in normalize_title(kk["title"]) or normalize_title(kk["title"]) in k_norm)
            for kk in kept_koho
        )
        if not is_dup:
            kept_koho.append(k)
        else:
            log.info("広報重複除外（新しい号・情報量優先）: '%s' を除外", k["title"])

    # issue_month は重複判定用。events.json には出さない
    return non_koho + [{key: v for key, v in k.items() if key != "issue_month"} for k in kept_koho]


def cleanup_koho_cache(cache: dict, today: dt.date) -> None:
    """終了済みイベントしか残っていない号は、events を空にして号の記録だけ残す（再処理を防ぐため）。"""
    today_iso = today.isoformat()
    changed = False
    for iss in cache.get("issues", {}).values():
        if is_koho_issue_done(iss):
            evs = iss.get("events", [])
            if evs and all(e.get("end_date", "") < today_iso for e in evs):
                log.info("%s のイベントは全件終了したため、events を空にして保持します。", iss.get("title"))
                iss["events"] = []
                iss["expired_cleared"] = True
                changed = True
    if changed:
        save_koho_cache(cache)


def report_koho_errors(cache: dict, today: dt.date, extra_errors: Optional[list[str]] = None) -> list[str]:
    """広報処理の異常を検出し、GitHub Actions のステップ出力に書き出す。"""
    errors = list(extra_errors or [])
    # 1. 一覧取得の失敗が3日続いたか
    last_ok = cache.get("last_list_ok_date")
    if last_ok:
        try:
            days_since = (today - dt.date.fromisoformat(last_ok)).days
            if days_since >= 3:
                errors.append(f"広報一覧の取得成功（{last_ok}）から{days_since}日経過しています")
        except Exception:
            pass
    else:
        errors.append("広報一覧の取得成功記録がありません")

    # 2. 最新号の issue_month が今月より2か月以上古いか
    issues = cache.get("issues", {})
    if issues:
        latest_month = max((iss.get("issue_month") or "") for iss in issues.values())
        if latest_month:
            try:
                iy, im = int(latest_month[:4]), int(latest_month[5:7])
                month_diff = (today.year - iy) * 12 + (today.month - im)
                if month_diff >= 2:
                    errors.append(f"広報最新号の発行年月（{latest_month}）が今月より{month_diff}か月古いです")
            except Exception:
                pass
    else:
        errors.append("広報キャッシュに号データがありません")

    # 3. 全ページ処理した号のイベントが0件
    for iss in issues.values():
        if not iss.get("total_pages"):
            errors.append(f"号（{iss.get('title')}）のPDFを読み込めていません")
        elif is_koho_issue_done(iss):
            if len(iss.get("events", [])) == 0 and not iss.get("expired_cleared"):
                errors.append(f"全ページ処理した号（{iss.get('title')}）のイベントが0件です")

    if not errors:
        return []

    for err in errors:
        log.warning("広報処理の異常検知: %s", err)

    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as f:
            f.write("koho_error=true\n")
            f.write(f"koho_error_detail={' / '.join(errors)}\n")

    return errors


def build_events(
    cache: dict[str, dict],
    form: dict,
    manual: dict,
    today: dt.date,
    koho_cache: Optional[dict] = None,
) -> list[dict]:
    # 1. 自動抽出分を dedupe_events() でまとめる
    events = dedupe_events([e["event"] for e in cache.values() if e.get("event")])
    # 2. 投稿フォーム分を足す
    events = merge_form(events, form)
    # 3. 広報分を足す
    if koho_cache:
        events = events + get_koho_events(koho_cache)
    # 4. 手動登録の上書き・非表示を適用する
    events = merge_manual(events, manual)
    # 5. drop_koho_duplicates() を実行する
    events = drop_koho_duplicates(events)

    today_iso = today.isoformat()
    events = [with_registration_defaults(e) for e in events if e["end_date"] >= today_iso]
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
    failures: list[str] = []
    manual = load_sheet(EVENTS_SHEET_CSV_URL, MANUAL_CACHE_PATH, "手動登録用シート（シート1）",
                        "manual", "m", failures)
    # 広報みなまた自動抽出
    # 広報の処理で例外が起きても、他の経路のイベントは更新する（壊れたPDFなど）
    koho_cache = load_koho_cache()
    koho_exception: list[str] = []
    try:
        koho_cache, _, _ = update_koho_cache(today, koho_cache)
        cleanup_koho_cache(koho_cache, today)
    except Exception as e:
        log.exception("広報の処理に失敗しました。")
        koho_exception.append(f"広報の処理で例外が発生しました: {type(e).__name__}")

    form = load_sheet(EVENTS_FORM_CSV_URL, FORM_CACHE_PATH, "投稿フォームの掲載用タブ",
                      "form", "f", failures)
    save_events(build_events(cache, form, manual, today, koho_cache))
    report_sheet_failures(failures)
    report_koho_errors(koho_cache, today, koho_exception)
    log.info("=== 完了 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
