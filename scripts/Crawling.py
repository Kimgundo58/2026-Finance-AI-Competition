# -*- coding: utf-8 -*-
"""
중소벤처기업진흥공단 창업지원(start.kosmes.or.kr) FAQ 전량 크롤러.

RAG 인덱싱용 JSON을 출력한다. 로그인/Selenium 불필요 —
FAQ 목록은 listFrm 폼을 pageIndex만 바꿔 POST하면 서버가 완성된 HTML을 돌려준다.
"""

import html
import json
import math
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://start.kosmes.or.kr"
LIST_URL = f"{BASE_URL}/yh_cus010_001.do"

PER_PAGE = 10
SLEEP_SEC = 0.7
TIMEOUT = 30
MAX_RETRY = 3

OUT_DIR = Path("kosmes_data")
OUT_JSON = OUT_DIR / "kosmes_faq.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": LIST_URL,
}


def fetch_page(session, page_index):
    """FAQ 목록 한 페이지를 POST로 받아 BeautifulSoup으로 돌려준다."""
    payload = {
        "pageIndex": str(page_index),
        "searchType": "1",
        "searchWord": "",
        "butxSlno": "",
        "searchButxTpCd": "",
        "searchButxMclCd": "",
        "searchButxSmclCd": "",
    }

    last_err = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            res = session.post(LIST_URL, data=payload, timeout=TIMEOUT)
            res.raise_for_status()
            res.encoding = "utf-8"
            return BeautifulSoup(res.text, "html.parser")
        except requests.RequestException as e:
            last_err = e
            wait = 2 * attempt
            print(f"  [retry {attempt}/{MAX_RETRY}] page {page_index} 실패: {e} → "
                  f"{wait}초 후 재시도", file=sys.stderr)
            time.sleep(wait)

    raise RuntimeError(f"page {page_index} 수집 실패") from last_err


def get_total_count(soup):
    """페이징 영역의 총 건수(span.total)를 읽는다."""
    node = soup.select_one(".pagingWrap .total")
    if node:
        digits = re.sub("[^0-9]", "", node.get_text())
        if digits:
            return int(digits)

    # 마크업이 바뀐 경우를 대비해 원문에서 직접 찾는다.
    m = re.search(r'class="total"[^>]*>\s*([\d,]+)', str(soup))
    if m:
        return int(m.group(1).replace(",", ""))
    return None


def clean_text(node):
    """<br>을 줄바꿈으로 살리면서 태그를 걷어낸 평문을 만든다."""
    if node is None:
        return ""

    raw = node.decode_contents()
    raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?i)</p\s*>", "\n", raw)
    text = BeautifulSoup(raw, "html.parser").get_text()
    text = html.unescape(text)
    text = text.replace("\xa0", " ")

    lines = [ln.strip() for ln in text.split("\n")]
    text = "\n".join(ln for ln in lines if ln)
    return text.strip()


def split_category(badge_text):
    """'사업비 / 사업비 일반' → ('사업비', '사업비 일반')"""
    parts = [p.strip() for p in badge_text.split("/") if p.strip()]
    if len(parts) >= 2:
        return parts[0], " / ".join(parts[1:])
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


def parse_attachments(item):
    """fn_egov_downFile(...) onclick에서 첨부파일 정보를 뽑는다."""
    files = []
    for a in item.select("[onclick*='fn_egov_downFile']"):
        onclick = a.get("onclick", "")
        args = re.findall(r"'([^']*)'", onclick)
        name = a.get("title") or a.get_text(strip=True)
        files.append({"name": name.strip(), "params": args})
    return files


def parse_items(soup, page_index):
    """li.unite 하나 = FAQ 한 건."""
    rows = []
    for order, item in enumerate(soup.select("ul.faqList li.unite"), start=1):
        badge = item.select_one("i.badge strong")
        badge_text = clean_text(badge).replace("\n", " ")
        badge_text = re.sub(r"\s+", " ", badge_text).strip()
        cat_main, cat_sub = split_category(badge_text)

        q_node = item.select_one(".expandTitle .title span.txt")
        question_raw = clean_text(q_node)

        a_node = item.select_one(".expandContent.faqContent .txt")
        if a_node:
            for hidden in a_node.select(".hiddenText"):
                hidden.decompose()
        answer_raw = clean_text(a_node)

        # 'Q1.' / 'A1.' 같은 접두어는 본문에서 뺀다.
        question = re.sub(r"^Q\s*\d*\s*[.)]\s*", "", question_raw).strip()
        answer = re.sub(r"^A\s*\d*\s*[.)]\s*", "", answer_raw).strip()

        if not question and not answer:
            continue

        rows.append({
            "id": f"kosmes-faq-p{page_index:03d}-{order:02d}",
            "category_main": cat_main,
            "category_sub": cat_sub,
            "question": question,
            "answer": answer,
            "question_raw": question_raw,
            "answer_raw": answer_raw,
            "attachments": parse_attachments(item),
            "page_index": page_index,
            "source": "중소벤처기업진흥공단 창업지원",
            "source_url": LIST_URL,
        })
    return rows


def crawl():
    session = requests.Session()
    session.headers.update(HEADERS)

    # 1페이지를 먼저 받아 총 건수와 총 페이지 수를 잡는다.
    print("[1/3] 첫 페이지 로드 및 전체 건수 확인...")
    first = fetch_page(session, 1)
    total = get_total_count(first)

    if total:
        total_pages = math.ceil(total / PER_PAGE)
        print(f"      전체 {total}건 / {total_pages}페이지")
    else:
        total_pages = 50
        print("      총 건수 파싱 실패 → 빈 페이지가 나올 때까지 순차 수집")

    print("[2/3] 페이지 수집 시작")
    records = parse_items(first, 1)
    print(f"      page 1: {len(records)}건")

    for page in range(2, total_pages + 1):
        time.sleep(SLEEP_SEC)
        soup = fetch_page(session, page)
        rows = parse_items(soup, page)
        print(f"      page {page}: {len(rows)}건")

        if not rows:
            print("      빈 페이지 도달 → 종료")
            break
        records.extend(rows)

    return records, total


def dedupe(records):
    """페이징이 흔들려 같은 문항이 두 번 잡히는 경우를 정리한다."""
    seen = set()
    unique = []
    for r in records:
        key = (r["question"], r["answer"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)
    return unique


def main():
    started = datetime.now()
    records, total = crawl()
    records = dedupe(records)

    print("[3/3] 저장")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    payload = {
        "meta": {
            "source": "중소벤처기업진흥공단 창업지원 FAQ",
            "source_url": LIST_URL,
            "crawled_at": started.isoformat(timespec="seconds"),
            "site_total_count": total,
            "collected_count": len(records),
        },
        "faqs": records,
    }

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"      저장 완료: {OUT_JSON.resolve()}")
    print(f"      수집 {len(records)}건 / 사이트 표기 {total}건")

    if total and len(records) != total:
        print(f"  [경고] 건수 불일치 (중복 제거 후 {len(records)}건). 중복 문항이 있거나 페이징이 변경됐을 수 있음.",
              file=sys.stderr)

    if records:
        s = records[0]
        print("\n--- 샘플 ---")
        print(f"분류 : {s['category_main']} / {s['category_sub']}")
        print(f"질문 : {s['question']}")
        print(f"답변 : {s['answer'][:120]}...")


if __name__ == "__main__":
    main()
