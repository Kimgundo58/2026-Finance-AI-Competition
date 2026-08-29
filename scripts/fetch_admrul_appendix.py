# -*- coding: utf-8 -*-
"""행정규칙 별표 수집 — `target=admbyl`.

법령은 별표 본문이 XML `별표단위/별표내용` 에 박스표로 들어오지만 **행정규칙은
본문에도 첨부에도 별표가 없는 경우가 많다.** 예: 「중소벤처기업부 보조사업 관리규정」은
본문이 "별표 1의 업종에는 보조사업비를 사용할 수 없다"고만 하고, 정작 그 업종 목록인
**「보조사업비 카드 사용제한 업종」**은 어디에도 없었다. 비목 적격성 판정에 직결되는 표다.

`lawSearch.do?target=admbyl` 이 별표를 독립 레코드로 준다(`별표서식파일링크` 포함).
다만 `query` 는 **별표명**만 매칭하므로 행정규칙명으로는 못 찾는다. 그래서 목록을
페이지네이션으로 훑어 인덱스를 만든 뒤 `관련행정규칙명` 으로 거른다.

실행:
    python scripts/fetch_admrul_appendix.py --index-only     목록만 수집(캐시)
    python scripts/fetch_admrul_appendix.py                  캐시 → 대상 별표 다운로드
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import Law_Crawling as L  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "법령 PDF" / "L1_법령"
OUT = SRC / "별표"
CACHE = ROOT / "법령 PDF" / "_admbyl_index.json"
SEARCH = "http://www.law.go.kr/DRF/lawSearch.do"
BAD = re.compile(r'[/\:*?"<>|]')
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def target_rules() -> dict[str, str]:
    """판정 인덱스에 편입된 행정규칙만 대상으로 한다."""
    src = json.loads((ROOT / "법령 PDF" / "_law_sources.json").read_text(encoding="utf-8"))
    return {v["name"]: k for k, v in src.items()
            if v.get("norm_type") == "행정규칙" and v.get("index", True)}


def fetch_page(page: int, tries: int = 3):
    """빈 응답이 간헐적으로 온다. 재시도하고, 그래도 안 되면 그 페이지만 건너뛴다."""
    for n in range(tries):
        try:
            r = requests.get(SEARCH, params={"OC": L.OC, "type": "XML", "target": "admbyl",
                                             "query": "*", "display": "100", "page": str(page)},
                             timeout=40)
            if not r.text.strip():
                raise ValueError("빈 응답")
            return ET.fromstring(r.text).findall(".//admrulbyl")
        except Exception as e:                                    # noqa: BLE001
            if n == tries - 1:
                print("  ! %d페이지 건너뜀 (%s)" % (page, str(e)[:40]))
                return None
            time.sleep(1.5 * (n + 1))
    return None


def build_index(max_pages: int) -> list[dict]:
    rows, page, empty_run, skipped = [], 1, 0, []
    while page <= max_pages:
        items = fetch_page(page)
        if items is None:
            skipped.append(page)
            page += 1
            continue
        if not items:
            empty_run += 1
            if empty_run >= 2:       # 연속 2회 비면 끝
                break
        else:
            empty_run = 0
            for it in items:
                rows.append({t: (it.findtext(t) or "").strip() for t in
                             ("별표일련번호", "별표명", "별표번호", "별표종류",
                              "관련행정규칙명", "관련행정규칙일련번호",
                              "소관부처명", "별표서식파일링크")})
        if page % 40 == 0:
            print("  ... %d페이지 / %d건" % (page, len(rows)), flush=True)
            CACHE.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        page += 1
        time.sleep(0.25)
    if skipped:
        print("  건너뛴 페이지 %d개: %s" % (len(skipped), skipped[:20]))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index-only", action="store_true")
    ap.add_argument("--max-pages", type=int, default=900)
    ap.add_argument("--refresh", action="store_true", help="캐시 무시하고 목록 재수집")
    args = ap.parse_args()

    if args.refresh or not CACHE.exists():
        print("별표 목록 수집 중 (전체 ~84,000건)")
        rows = build_index(args.max_pages)
        CACHE.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        print("목록 %d건 저장 → %s" % (len(rows), CACHE.name))
    else:
        rows = json.loads(CACHE.read_text(encoding="utf-8"))
        print("캐시 목록 %d건" % len(rows))
    if args.index_only:
        return

    want = target_rules()
    hits = [r for r in rows if r["관련행정규칙명"] in want]
    print("대상 행정규칙 %d건 → 매칭된 별표 %d개\n" % (len(want), len(hits)))

    OUT.mkdir(parents=True, exist_ok=True)
    log, ok, fail, skip = [], 0, 0, 0
    for i, h in enumerate(hits, 1):
        link = h["별표서식파일링크"]
        if not link:
            skip += 1
            continue
        if link.startswith("/"):
            link = "http://www.law.go.kr" + link
        dest = OUT / ("BYL_%s_%s_%s__%s.hwp" % (
            BAD.sub("_", h["관련행정규칙명"])[:34], h["별표번호"],
            h["별표일련번호"], BAD.sub("_", h["별표명"])[:40]))
        if dest.exists():
            skip += 1
            continue
        try:
            resp = requests.get(link, headers=UA, timeout=60, allow_redirects=True)
            resp.raise_for_status()
            # ⚠️ 크기만 검사하면 안 된다 (2026-08-27 실측).
            #    다운로드가 간헐 실패하면 수 KB 짜리 **HTML 오류페이지**가 오는데
            #    512바이트를 넘어서 통과해 버린다. 5건이 그렇게 섞여 들어왔다.
            #    매직바이트로 실제 포맷을 확인한다. 재시도하면 대부분 정상적으로 온다.
            head = resp.content[:8]
            if head[:8] == bytes.fromhex("d0cf11e0a1b11ae1"):
                fmt = "HWP"
            elif head[:2] == b"PK":
                fmt = "HWPX"          # 확장자는 .hwp 인데 실제로는 HWPX 인 게 다수다
            elif head[:5] == b"%PDF-":
                fmt = "PDF"
            else:
                raise RuntimeError("HWP/HWPX 가 아님(%d바이트, %s) — 재시도 필요"
                                   % (len(resp.content),
                                      resp.headers.get("Content-Type", "?")[:30]))
            dest.write_bytes(resp.content)
            ok += 1
            print("[%3d/%d] OK %7.1f KB  %s" % (i, len(hits), len(resp.content) / 1024,
                                                dest.name[:76]))
            log.append({**h, "file": str(dest.relative_to(ROOT)).replace("\\", "/"),
                        "bytes": len(resp.content), "format": fmt, "status": "ok"})
        except Exception as e:                                   # noqa: BLE001
            fail += 1
            print("[%3d/%d] FAIL %s <- %s" % (i, len(hits), h["별표명"][:34], e))
            log.append({**h, "status": "fail", "error": str(e)})
        time.sleep(0.35)

    (OUT / "_별표_수집로그.json").write_text(
        json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n성공 %d / 실패 %d / 건너뜀 %d" % (ok, fail, skip))


if __name__ == "__main__":
    main()
