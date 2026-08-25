#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
水俣市 世代別生活情報ナビ 自動更新スクリプト
-------------------------------------------------
1. 水俣市公式サイトの「新着情報」（全ジャンル横断）と「防災サイトのお知らせ」から
   記事候補をできるだけ広く（カテゴリで事前に絞り込まず）収集する
2. Gemini API で、
   - 住民・事業者の生活に関係する具体的な情報かどうかを判定
   - 該当する世代タグ・ジャンルタグを付与
   - 要約を生成
3. public/data/life_info.json を更新（重複は source_url をキーにマージ）

「宇城市 支援情報自動更新ポータル」のscripts/update_data.pyをベースに、
・カテゴリ事前フィルタを廃止し、取りこぼしを防ぐ
・「世代タグ」「ジャンルタグ」の2軸タグ付けに変更
した派生版。

環境変数:
    GEMINI_API_KEY  ... Google AI Studio の Gemini API キー（必須）

実行方法:
    python scripts/update_data.py
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

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import tz

# --------------------------------------------------------------------------
# 基本設定
# --------------------------------------------------------------------------
ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT_DIR / "public" / "data" / "life_info.json"
JST = tz.gettz("Asia/Tokyo")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
GEMINI_ENDPOINT = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", "0"))  # 0=フィルタなし
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "400"))
GEMINI_MAX_RETRIES = int(os.environ.get("GEMINI_MAX_RETRIES", "5"))
# 候補間の待機秒数。AI StudioのRate limits画面で実際に確認したところ、
# gemini-3.5-flash-lite は無料枠で 15 RPM（RPDは500で余裕あり）だった
# （2026-08-24、実運用で確認）。15 RPM = 4秒間隔ペースなので、余裕を持って
# 4.5秒とし、リトライは「ペース調整をすり抜けた分の保険」に留める。
GEMINI_REQUEST_INTERVAL_SEC = float(os.environ.get("GEMINI_REQUEST_INTERVAL_SEC", "4.5"))
# リトライ（最大GEMINI_MAX_RETRIES回・指数バックオフ）をすべて使い切ってなお失敗する状態が
# 連続した場合、1分単位のレート制限(RPM)ではなく1日の上限(RPD)等の恒久的な問題である
# 可能性が高い。そのまま全候補を回すと1件あたり最大約90秒を無駄にし続けるため、
# 一定回数連続したら残りを打ち切り、ここまでの結果を保存する
# （2026-08-24、RPM対応後も134件目以降ずっと429が続く事象で発生を確認）。
GEMINI_CONSECUTIVE_FAILURE_LIMIT = int(os.environ.get("GEMINI_CONSECUTIVE_FAILURE_LIMIT", "3"))

REQUEST_TIMEOUT = 20
# HTTPヘッダーはASCII(latin-1)のみ許容されるため、日本語を含めないこと。
USER_AGENT = (
    "MinamataLifeInfoBot/1.0 "
    "(+https://github.com/)"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("update_data")


# --------------------------------------------------------------------------
# 収集対象ソース
#   カテゴリで事前に絞り込まず、「新着情報」を丸ごと拾うのが基本方針。
#   関連性の判定・タグ付けはすべてGemini側で行う。
# --------------------------------------------------------------------------
@dataclass
class Source:
    name: str
    url: str
    type: str  # "rss" | "html"
    list_selector: Optional[str] = None
    base_url: Optional[str] = None


SOURCES: list[Source] = [
    # メインの新着情報（全ジャンル横断。環境サイト・スポーツサイト等の投稿も一部含まれる）
    Source(
        name="水俣市 新着情報",
        url="https://www.city.minamata.lg.jp/new_list.html",
        type="html",
        list_selector="a",
        base_url="https://www.city.minamata.lg.jp",
    ),
    # 防災サイトのお知らせ（災害・防災関連は専用サイトで先に・頻繁に更新されるため別途収集）
    Source(
        name="水俣市 防災サイトのお知らせ",
        url="https://www.city.minamata.lg.jp/bousai/list00500.html",
        type="html",
        list_selector="a",
        base_url="https://www.city.minamata.lg.jp",
    ),
    # 水俣市 スポーツ情報の新着（RSSが提供されているためRSSとして取得。
    # 「水俣市 新着情報」にも一部重複するが、source_urlキーでマージされるため問題ない）
    Source(
        name="水俣市 スポーツ情報",
        url="https://www.city.minamata.lg.jp/sports/new_list.xml",
        type="rss",
    ),
    # みなまた観光物産協会 イベント情報（市公式サイトの新着情報フィードに載らない、
    # 独自サイト発のイベント告知を拾うために追加。2026-08-23、吉野さんからの指摘・
    # Antigravity提案を受けてClaudeが調査。
    # サイトはJoomla製で、記事一覧は `.article-header` 配下に <a> を持つ構造
    # （実HTMLで確認済み。1ページあたり3件のみ表示されるブログレイアウトのため、
    # 日次収集を続けることで新着分を取りこぼしなく拾える）。
    # ページ内の関連記事ウィジェット（`.com-content-blog__link`）は掲載時期が
    # 不定でノイズになりやすいため、意図的に対象外としている。
    Source(
        name="みなまた観光物産協会 イベント情報",
        url="https://www.go-minamata.jp/news-events/",
        type="html",
        list_selector=".article-header a",
        base_url="https://www.go-minamata.jp",
    ),
    Source(
        name="みなまた観光物産協会 お知らせ",
        url="https://www.go-minamata.jp/news-information/",
        type="html",
        list_selector=".article-header a",
        base_url="https://www.go-minamata.jp",
    ),
]

# 事前フィルタは「取りこぼし防止」のため最小限にする。
# ここでは、明らかにナビゲーションメニュー等のノイズだけをタイトルの短さで弾き、
# 「住民の生活に関係する情報かどうか」の実質的な判定はすべてGeminiに委ねる。
MIN_TITLE_LENGTH = 6


# --------------------------------------------------------------------------
# データ構造
# --------------------------------------------------------------------------
@dataclass
class Candidate:
    title: str
    link: str
    snippet: str
    published_date: Optional[str]
    source_name: str


def make_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def http_get(url: str) -> Optional[requests.Response]:
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning("取得失敗: %s (%s)", url, e)
        return None

    # 水俣市サイトはレスポンスヘッダーにcharsetを明示しないため、requestsが
    # HTTP仕様上のデフォルトである ISO-8859-1 に誤判定し、日本語が文字化けする
    # （life_info.jsonのtitleが文字化けする不具合の原因。2026-08-24、実運用で発覚）。
    #
    # 当初は resp.apparent_encoding（chardet/charset_normalizerによる推定）で
    # 上書きしていたが、ローカル環境では正しく "UTF-8-SIG" と推定される一方、
    # GitHub Actions（Linux）環境では別の結果になったらしく、実運用で文字化けが
    # 再発した。生バイトを直接確認したところ、水俣市サイトは例外なく
    # UTF-8（先頭にBOM付き, b'\xef\xbb\xbf'）で配信されていることを確認済みのため、
    # 推定に頼らず固定値で上書きする（環境差によるブレをなくすため）。
    if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = "utf-8-sig"

    return resp


# 水俣市サイト（city.minamata.lg.jp）のCMSは、記事ページ本文の冒頭付近に
#   <div class="updDate">最終更新日：<time datetime="2024-11-28T16:44:12+09:00">...</time></div>
# という構造化マークアップを出力する（monitor_stock.pyで2026-08-23に実HTMLで確認済み）。
# 新着一覧ページ（new_list.html等）の側では日付が併記されないリンクが大半
# （実データで191件中187件がpublished_date欠落）だったため、Geminiに「関係あり」と
# 判定された項目についてのみ、記事ページ本体から正確な日付を取得し直す。
# なお観光物産協会サイト（go-minamata.jp）は記事ページ自体に日付表示が無いことを
# 実際に確認済みのため、対象を水俣市公式サイトのドメインに限定する。
ARTICLE_DATE_DOMAIN = "minamata.lg.jp"
ARTICLE_DATE_TEXT_PATTERN = re.compile(
    r"最終更新日\s*[:：]\s*(\d{4})年(\d{1,2})月(\d{1,2})日"
)


def fetch_article_published_date(url: str) -> Optional[str]:
    if ARTICLE_DATE_DOMAIN not in url:
        return None

    resp = http_get(url)
    if resp is None:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    time_tag = soup.select_one("time[datetime]")
    if time_tag is not None:
        raw = (time_tag.get("datetime") or "").strip()
        if raw:
            try:
                return dt.datetime.fromisoformat(raw).date().isoformat()
            except ValueError:
                log.warning("記事ページのtime要素のdatetime属性のパースに失敗: %r (%s)", raw, url)

    # フォールバック: 構造化マークアップが見つからない場合、可視テキストから抽出する。
    text = soup.get_text(" ", strip=True)
    m = ARTICLE_DATE_TEXT_PATTERN.search(text)
    if not m:
        return None
    y, mo, d = (int(g) for g in m.groups())
    try:
        return dt.date(y, mo, d).isoformat()
    except ValueError:
        return None


def fetch_rss(source: Source) -> Optional[list[Candidate]]:
    log.info("RSS取得中: %s", source.url)
    resp = http_get(source.url)
    if resp is None:
        return None

    feed = feedparser.parse(resp.content)
    candidates: list[Candidate] = []
    for entry in feed.entries:
        title = getattr(entry, "title", "").strip()
        link = getattr(entry, "link", "").strip()
        snippet = getattr(entry, "summary", "") or getattr(entry, "description", "")
        snippet = BeautifulSoup(snippet, "html.parser").get_text(" ", strip=True)

        published_date = None
        if getattr(entry, "published_parsed", None):
            published_date = dt.date(*entry.published_parsed[:3]).isoformat()
        elif getattr(entry, "updated_parsed", None):
            published_date = dt.date(*entry.updated_parsed[:3]).isoformat()

        if not title or not link:
            continue

        candidates.append(
            Candidate(title, link, snippet, published_date, source.name)
        )
    log.info("  -> %d 件取得", len(candidates))
    return candidates


DATE_PATTERN = re.compile(r"(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})")


def guess_date_from_text(text: str) -> Optional[str]:
    m = DATE_PATTERN.search(text)
    if not m:
        return None
    y, mo, d = (int(g) for g in m.groups())
    try:
        return dt.date(y, mo, d).isoformat()
    except ValueError:
        return None


def fetch_html(source: Source) -> Optional[list[Candidate]]:
    log.info("HTML取得中: %s", source.url)
    resp = http_get(source.url)
    if resp is None:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    anchors = soup.select(source.list_selector or "a")

    candidates: list[Candidate] = []
    seen_links: set[str] = set()
    for a in anchors:
        title = a.get_text(" ", strip=True)
        href = a.get("href")
        if not title or not href:
            continue
        if len(title) < MIN_TITLE_LENGTH:
            continue

        link = requests.compat.urljoin(source.base_url or source.url, href)
        if link in seen_links:
            continue
        seen_links.add(link)

        parent_text = a.parent.get_text(" ", strip=True) if a.parent else ""
        published_date = guess_date_from_text(parent_text) or guess_date_from_text(title)

        candidates.append(
            Candidate(title, link, parent_text[:300], published_date, source.name)
        )
    log.info("  -> %d 件取得（フィルタ前）", len(candidates))
    return candidates


def within_age_limit(candidate: Candidate) -> bool:
    if MAX_AGE_DAYS <= 0 or not candidate.published_date:
        return True
    try:
        d = dt.date.fromisoformat(candidate.published_date)
    except ValueError:
        return True
    return (dt.date.today() - d).days <= MAX_AGE_DAYS


def collect_all_candidates() -> tuple[list[Candidate], int, int]:
    all_candidates: list[Candidate] = []
    sources_ok = 0

    for source in SOURCES:
        try:
            items = fetch_rss(source) if source.type == "rss" else fetch_html(source)
        except Exception:  # noqa: BLE001
            log.exception("ソース処理中にエラー: %s", source.name)
            items = None

        if items is not None:
            sources_ok += 1
        else:
            items = []

        filtered = [c for c in items if within_age_limit(c)]
        log.info("  -> 候補として %d 件を採用 (%s)", len(filtered), source.name)
        all_candidates.extend(filtered)
        time.sleep(1)

    dedup: dict[str, Candidate] = {}
    for c in all_candidates:
        dedup.setdefault(c.link, c)
    return list(dedup.values()), sources_ok, len(SOURCES)


# --------------------------------------------------------------------------
# Gemini API による判定・タグ付け・要約
# --------------------------------------------------------------------------
AUDIENCE_TAGS = ["子育て・妊娠期", "学生・若者", "現役世代", "高齢者", "障がいのある方", "事業者向け", "全世代"]
TOPIC_TAGS = [
    "子育て", "健康・医療", "介護・福祉", "税金・年金", "住まい", "防災・災害",
    "イベント", "就労・仕事", "くらしの手続き", "教育・生涯学習", "募集・相談",
    "産業・事業支援", "交通", "環境",
]

EXTRACTION_SYSTEM_PROMPT = f"""\
あなたは、熊本県水俣市の「世代別生活情報ポータルサイト」の編集アシスタントです。
市の公式サイトの新着情報から、住民や事業者の生活に関係する具体的な情報を抽出します。

以下に渡す「タイトル」「抜粋」「リンク」の情報を読み、この記事が
住民・事業者にとって実際に役立つ生活情報かどうかを判定してください。

【除外してよいもの】
- 入札公告・条件付一般競争入札・公募型プロポーザル・契約に関する内部事務手続き
- 監査結果・住民監査請求・財政状況・予算・例規改正など、行政の内部手続きのみが対象で、
  住民の行動を特に必要としないもの
- 内容が完全に重複する道路工事等の入札関連情報

【除外せず、基本的に含めるべきもの】
- 子育て、健康・医療、介護、福祉、税金、住まい、防災・災害、イベント、募集、相談窓口、
  就労支援、産業・事業者支援、教育、くらしの手続き、施設案内 など、住民や事業者が
  知って行動できる情報全般
- 職員採用試験の情報（就職を考えている学生・若者にとって有用なため含める）
- 迷った場合は、除外するより含める方を優先してください（取りこぼしを避けるため）。

該当しない場合は "is_relevant": false を返し、他のフィールドは null または空配列にしてください。

該当する場合は、日本語で簡潔に要約し、以下のJSON形式で**JSONのみ**を出力してください。
説明文やMarkdownのコードブロック記号は一切付けないでください。

{{
  "is_relevant": true,
  "audience_tags": [{", ".join(f'"{t}"' for t in AUDIENCE_TAGS)} のうち該当するものを1つ以上],
  "topic_tags": [{", ".join(f'"{t}"' for t in TOPIC_TAGS)} のうち該当するものを1つ以上],
  "organization": "実施主体（分かる範囲で。不明なら「水俣市」）",
  "summary": "2〜3文程度の日本語要約。金額・期限・対象者などの具体情報があれば含める。",
  "target": "対象者（分かる範囲で1文）",
  "deadline": "申請期限が明記されていればYYYY-MM-DD形式。不明ならnull"
}}

audience_tagsの判断基準:
- 子育て・妊娠期: 妊娠中の方、乳幼児〜高校生までの子を持つ保護者向け
- 学生・若者: 概ね15〜30歳の学生・若者本人向け（進学・就職・二十歳の集い等）
- 現役世代: 概ね20〜64歳の働く世代向け（子育て・高齢者・学生に該当しない一般的な内容も含む）
- 高齢者: 概ね65歳以上、介護保険・年金等
- 障がいのある方: 障害者手帳、障害福祉サービス等
- 事業者向け: 中小企業・商店・個人事業主向けの支援・手続き
- 全世代: 特定の世代に絞られない、住民全体向けの情報（防災、税、選挙、施設案内等）
複数該当する場合はすべて含めてください。
"""


def call_gemini(candidate: Candidate) -> Optional[dict[str, Any]]:
    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY が未設定のため、判定をスキップします。")
        return None

    user_content = (
        f"タイトル: {candidate.title}\n"
        f"抜粋: {candidate.snippet}\n"
        f"リンク: {candidate.link}\n"
        f"情報源: {candidate.source_name}\n"
        f"取得できた日付: {candidate.published_date or '不明'}\n"
    )

    payload = {
        "system_instruction": {"parts": [{"text": EXTRACTION_SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_content}]}],
        "generationConfig": {
            "temperature": 0.2,
            "response_mime_type": "application/json",
        },
    }

    # APIキーはURLのクエリパラメータではなくヘッダー（x-goog-api-key）で渡す。
    # クエリパラメータに含めると、HTTPエラー時の例外メッセージにURL全体が
    # 含まれてしまい、ログ出力経由でキーが漏れるリスクがあるため。
    headers = {"x-goog-api-key": GEMINI_API_KEY}

    # 429（レート制限）は一時的なもので、そのまま諦めると「関係ない記事」と
    # 同じ扱いで候補が失われてしまう（2026-08-24、実運用で大量発生を確認）。
    # 指数バックオフで再試行し、Retry-Afterヘッダーがあればそれを優先する。
    resp: Optional[requests.Response] = None
    for attempt in range(GEMINI_MAX_RETRIES + 1):
        try:
            resp = requests.post(
                GEMINI_ENDPOINT, headers=headers, json=payload, timeout=REQUEST_TIMEOUT
            )
        except requests.RequestException as e:
            log.warning("Gemini API 呼び出し失敗: %s (%s)", candidate.title, e)
            return None

        if resp.status_code == 429 and attempt < GEMINI_MAX_RETRIES:
            retry_after = resp.headers.get("Retry-After")
            try:
                wait_s = float(retry_after) if retry_after else min(2.0 * (2 ** attempt), 30.0)
            except ValueError:
                wait_s = min(2.0 * (2 ** attempt), 30.0)
            log.warning(
                "Gemini APIがレート制限中(429): %s。%.1f秒待って再試行します (%d/%d)",
                candidate.title, wait_s, attempt + 1, GEMINI_MAX_RETRIES,
            )
            time.sleep(wait_s)
            continue
        break

    try:
        resp.raise_for_status()
    except requests.RequestException as e:
        # 429の場合は応答本文に「どの上限（RPM/RPD/TPM等）に達したか」が入っていることが
        # 多いため、原因切り分けのためにログへ残す（2026-08-24、RPM対応後もRPDらしき
        # 恒久的な429が発生したため追加）。
        if resp.status_code == 429:
            log.warning(
                "Gemini API 呼び出し失敗: %s (%s) / 応答本文: %s",
                candidate.title, e, resp.text[:500],
            )
        else:
            log.warning("Gemini API 呼び出し失敗: %s (%s)", candidate.title, e)
        return None

    data = resp.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        log.warning("Gemini応答の形式が想定外です: %s", data)
        return None

    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        log.warning("Gemini応答のJSONパースに失敗: %s", text[:200])
        return None


def structure_candidate(candidate: Candidate) -> tuple[Optional[dict[str, Any]], bool]:
    """(生活情報として採用する項目 or None, Geminiから何らかの回答を得られたか) を返す。

    2つ目の値は「関係ない記事として判定できた」候補と「API呼び出し自体が失敗した」候補を
    区別するために使う。前者は次回以降の判定をスキップしてよいが、後者は一時的な失敗
    （レート制限等）の可能性があるため、次回また判定し直す必要がある。
    """
    result = call_gemini(candidate)
    if result is None:
        return None, False
    if not result.get("is_relevant"):
        return None, True

    now = dt.datetime.now(tz=JST).isoformat()
    item = {
        "id": make_id(candidate.link),
        "title": candidate.title,
        "audience_tags": result.get("audience_tags") or ["全世代"],
        "topic_tags": result.get("topic_tags") or [],
        "organization": result.get("organization") or "水俣市",
        "summary": result.get("summary") or candidate.snippet[:200],
        "target": result.get("target"),
        "deadline": result.get("deadline"),
        "source_url": candidate.link,
        "source_name": candidate.source_name,
        "published_date": candidate.published_date,
        "collected_at": now,
        "status": "active",
    }
    return item, True


# --------------------------------------------------------------------------
# 既存JSONとのマージ・保存
# --------------------------------------------------------------------------
def load_existing() -> dict[str, Any]:
    if DATA_PATH.exists():
        with DATA_PATH.open(encoding="utf-8") as f:
            return json.load(f)
    return {"last_updated": None, "items": []}


# 「関係ない記事」と判定済みのURL（ナビゲーションメニュー等、毎回候補に出てくるが
# 恒久的に無関係なもの）を記録し、次回以降はGemini判定自体をスキップする。
# life_info.json は「採用された」項目しか保持しないため、これとは別に管理する必要がある。
# API呼び出し自体が失敗した候補（レート制限等）はここに含めず、次回また判定し直す。
CHECKED_URLS_PATH = ROOT_DIR / "public" / "data" / "_checked_urls.json"


def load_checked_urls() -> set[str]:
    if CHECKED_URLS_PATH.exists():
        with CHECKED_URLS_PATH.open(encoding="utf-8") as f:
            return set(json.load(f).get("urls", []))
    return set()


def save_checked_urls(urls: set[str]) -> None:
    CHECKED_URLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = CHECKED_URLS_PATH.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump({"urls": sorted(urls)}, f, ensure_ascii=False, indent=2)
    tmp_path.replace(CHECKED_URLS_PATH)


def sort_key(item: dict) -> str:
    # published_date（記事本文から読み取れた日付）が最優先。多くの制度案内ページ等は
    # 本文中に明確な日付表記がなくpublished_dateがnullになる（2026-08-25、実データで
    # 191件中187件がnullと判明）。その場合はcollected_at（このスクリプトが最初に
    # 見つけた日時）を代わりに使い、「新着」という見た目の意味を保つ。
    return item.get("published_date") or item.get("collected_at") or ""


def merge_items(existing_items: list[dict], new_items: list[dict]) -> list[dict]:
    by_url: dict[str, dict] = {item["source_url"]: item for item in existing_items}
    for item in new_items:
        by_url[item["source_url"]] = item
    merged = list(by_url.values())
    merged.sort(key=sort_key, reverse=True)
    return merged[:MAX_ITEMS]


def save_data(items: list[dict]) -> None:
    now = dt.datetime.now(tz=JST).isoformat()
    payload = {
        "last_updated": now,
        "generated_by": "scripts/update_data.py",
        "items": items,
    }
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = DATA_PATH.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp_path.replace(DATA_PATH)
    log.info("保存完了: %s (%d件)", DATA_PATH, len(items))


def main() -> int:
    log.info("=== 生活情報の収集を開始します ===")

    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY が設定されていません。")
        return 1

    candidates, sources_ok, sources_total = collect_all_candidates()
    log.info(
        "収集された候補（重複排除後）: %d 件 / アクセス成功ソース: %d/%d",
        len(candidates), sources_ok, sources_total,
    )

    if sources_ok == 0:
        log.error("すべての情報源へのアクセスに失敗しました。")
        return 1

    if not candidates:
        log.info("本日は新規候補がありませんでした（異常ではありません）。終了します。")
        return 0

    checked_urls = load_checked_urls()
    unseen = [c for c in candidates if c.link not in checked_urls]
    log.info(
        "うち未判定の候補: %d 件（判定済みとしてスキップ: %d 件）",
        len(unseen), len(candidates) - len(unseen),
    )

    if not unseen:
        log.info("新規に判定すべき候補がありませんでした。終了します。")
        return 0

    new_items: list[dict] = []
    newly_checked: set[str] = set()
    consecutive_failures = 0
    for i, candidate in enumerate(unseen, start=1):
        log.info("[%d/%d] Gemini判定中: %s", i, len(unseen), candidate.title)
        structured, answered = structure_candidate(candidate)
        if structured:
            if not structured.get("published_date"):
                real_date = fetch_article_published_date(candidate.link)
                if real_date:
                    structured["published_date"] = real_date
            new_items.append(structured)
        if answered:
            newly_checked.add(candidate.link)
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            if consecutive_failures >= GEMINI_CONSECUTIVE_FAILURE_LIMIT:
                log.warning(
                    "Gemini APIへの問い合わせが%d件連続でリトライ上限まで失敗しました。"
                    "レート制限ではなく1日の上限等、恒久的な問題の可能性が高いため、"
                    "残り%d件の判定を打ち切り、ここまでの結果を保存します。",
                    consecutive_failures, len(unseen) - i,
                )
                break
        time.sleep(GEMINI_REQUEST_INTERVAL_SEC)

    log.info("生活情報として採用: %d 件", len(new_items))

    existing = load_existing()
    merged = merge_items(existing.get("items", []), new_items)
    save_data(merged)
    save_checked_urls(checked_urls | newly_checked)

    log.info("=== 完了 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
