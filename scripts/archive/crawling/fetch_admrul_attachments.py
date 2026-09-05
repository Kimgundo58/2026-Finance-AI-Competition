# -*- coding: utf-8 -*-
"""행정규칙 별표·서식 첨부 수집기.

법령(`조문단위` 있는 XML)은 별표 본문이 `별표단위/별표내용`에 ASCII 박스표로
그대로 들어오므로 대상이 아니다. 행정규칙은 XML에 별표 본문이 없고 조문에서
"별표 1에 따른다"고 참조만 하므로, 표 자체는 `첨부파일` 링크에만 있다.

따라서 **본문이 별표·별지·서식을 참조하는 행정규칙**만 골라 첨부를 받는다.
전량 받으면 제개정이유서 같은 쓸모없는 문서까지 딸려온다.

실행:
    python scripts/archive/crawling/fetch_admrul_attachments.py --dry-run   대상만 출력
    python scripts/archive/crawling/fetch_admrul_attachments.py             수집
"""
from __future__ import annotations

# 🔴 2026-09-05 scripts/archive/ 이관 — 원래 scripts/ 바로 밑에 있던 파일이라
#    아래(또는 이 파일의 기존 sys.path 계산)는 scripts/ 바로 밑 기준으로 짜여 있다.
#    이관으로 깊이가 늘어나 깨지므로, `scripts/_lib` 을 찾을 때까지 위로 걸어 올라가
#    scripts/ 와 프로젝트 루트를 sys.path 맨 앞에 다시 건다.
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
# 🔴 archive 내부에서 카테고리를 넘나드는 import(예: index_guard, stage0_run)가
#    있어 scripts/archive/ 의 모든 하위 폴더도 같이 건다.
_archive_이관 = _os_이관.path.join(_p_이관, "archive")
if _os_이관.path.isdir(_archive_이관):
    for _d_이관 in _os_이관.listdir(_archive_이관):
        _full_이관 = _os_이관.path.join(_archive_이관, _d_이관)
        if _os_이관.path.isdir(_full_이관) and _full_이관 not in _sys_이관.path:
            _sys_이관.path.insert(0, _full_이관)


import argparse
import json
import os
import re
import time
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "scripts" / "_lib").is_dir())  # 🔴 2026-09-05 archive 이관 — 깊이 무관 계산으로 교체
SRC = ROOT / "법령 PDF" / "L1_법령"
OUT = SRC / "첨부"

REF = re.compile(r"(별표|별지|서식)")
BAD = re.compile(r'[/\:*?"<>|]')
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def safe(s: str) -> str:
    return BAD.sub("_", s).strip()


def scan() -> list[dict]:
    """행정규칙 XML 을 훑어 별표 참조 여부와 첨부 목록을 뽑는다."""
    rows = []
    for f in sorted(SRC.glob("*.xml")):
        root = ET.parse(f).getroot()
        if root.findall(".//조문단위"):
            continue                       # 법령 → 별표가 본문에 있음
        body = "\n".join((c.text or "") for c in root if c.tag == "조문내용")
        info = root.find(".//행정규칙기본정보")

        def g(tag: str) -> str:
            e = info.find(tag) if info is not None else None
            return (e.text or "").strip() if e is not None and e.text else ""

        # 첨부파일명/첨부파일링크가 형제로 번갈아 나온다. 쌍으로 묶어야 한다.
        atts = []
        for a in root.findall(".//첨부파일"):
            kids = list(a)
            for i in range(0, len(kids) - 1, 2):
                if kids[i].tag == "첨부파일명" and kids[i + 1].tag == "첨부파일링크":
                    atts.append(((kids[i].text or "").strip(),
                                 (kids[i + 1].text or "").strip()))
        rows.append({"file": f.name, "name": g("행정규칙명"), "id": g("행정규칙ID"),
                     "kind": g("행정규칙종류"), "eff": g("시행일자"),
                     "refs": sorted({m.group(0) for m in REF.finditer(body)}),
                     "n_ref": len(REF.findall(body)), "atts": atts})
    return rows


def plan_downloads(rows: list[dict]) -> list[tuple[dict, str, str]]:
    """PDF 를 우선한다 — HWP 는 파싱 단계가 하나 더 붙는다."""
    out = []
    for r in rows:
        if not (r["refs"] and r["atts"]):
            continue
        pdfs = [(n, l) for n, l in r["atts"] if n.lower().endswith(".pdf")]
        for n, l in (pdfs or r["atts"][:1]):
            out.append((r, n, l))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows = scan()
    todo = plan_downloads(rows)
    hit = sum(1 for r in rows if r["refs"] and r["atts"])
    print("행정규칙 %d건 / 별표·서식 참조 %d건 / 내려받을 첨부 %d개"
          % (len(rows), hit, len(todo)))
    if args.dry_run:
        for r, n, _ in todo:
            print("  %-44s <- %s" % (r["name"][:44], n[:60]))
        return

    OUT.mkdir(parents=True, exist_ok=True)
    log, ok, fail = [], 0, 0
    for i, (r, name, link) in enumerate(todo, 1):
        dest = OUT / ("ADM_%s_%s__%s" % (r["id"], safe(r["name"])[:40], safe(name)))
        rec = {"rule": r["name"], "adm_id": r["id"], "att": name, "url": link}
        try:
            resp = requests.get(link, headers=UA, timeout=60, allow_redirects=True)
            resp.raise_for_status()
            body = resp.content
            if len(body) < 1024:            # 오류 페이지가 200으로 오는 경우가 있다
                raise RuntimeError("응답이 %d바이트뿐 — 파일이 아님" % len(body))
            dest.write_bytes(body)
            ok += 1
            print("[%2d/%d] OK   %7.1f KB  %s" % (i, len(todo), len(body) / 1024, dest.name))
            rec.update(status="ok", bytes=len(body), file=str(dest.relative_to(ROOT)))
        except Exception as e:                              # noqa: BLE001
            fail += 1
            print("[%2d/%d] FAIL %s <- %s" % (i, len(todo), name[:50], e))
            rec.update(status="fail", error=str(e))
        log.append(rec)
        time.sleep(0.4)

    (OUT / "_첨부_수집로그.json").write_text(
        json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n성공 %d / 실패 %d" % (ok, fail))


if __name__ == "__main__":
    main()
