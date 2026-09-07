# -*- coding: utf-8 -*-
"""행정규칙 별표 수집 — `lawSearch.do?target=admbyl` 목록을 전부 훑어 캐시한 뒤
`관련행정규칙명` 으로 대상 행정규칙의 별표 파일을 내려받는다 (query 는 별표명만 매칭한다).

실행:
    python scripts/archive/crawling/fetch_admrul_appendix.py --index-only     목록만 수집(캐시)
    python scripts/archive/crawling/fetch_admrul_appendix.py                  캐시 → 대상 별표 다운로드
"""
from __future__ import annotations

# scripts/_lib 을 찾을 때까지 위로 올라가 scripts/ 와 프로젝트 루트를 sys.path 맨 앞에 건다.
import os as _os_이관, sys as _sys_이관
_p_이관 = _os_이관.path.dirname(_os_이관.path.abspath(__file__))
while not _os_이관.path.isdir(_os_이관.path.join(_p_이관, "_lib")):
    _parent_이관 = _os_이관.path.dirname(_p_이관)
    if _parent_이관 == _p_이관:
        break
    _p_이관 = _parent_이관
if _p_이관 not in _sys_이관.path:
    _sys_이관.path.insert(0, _p_이관)
if _os_이관.path.dirname(_p_이관) not in _sys_이관.path:
    _sys_이관.path.insert(0, _os_이관.path.dirname(_p_이관))
# archive 하위 폴더끼리 서로 import 하므로 scripts/archive/* 도 건다.
_archive_이관 = _os_이관.path.join(_p_이관, "archive")
if _os_이관.path.isdir(_archive_이관):
    for _d_이관 in _os_이관.listdir(_archive_이관):
        _full_이관 = _os_이관.path.join(_archive_이관, _d_이관)
        if _os_이관.path.isdir(_full_이관) and _full_이관 not in _sys_이관.path:
            _sys_이관.path.insert(0, _full_이관)


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

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "scripts" / "_lib").is_dir())
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
            r = requests.get(SEARCH, params={"OC": L.require_oc(), "type": "XML", "target": "admbyl",
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
            # 크기 검사만으론 HTML 오류페이지가 통과한다 — 매직바이트로 포맷을 확인한다.
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
