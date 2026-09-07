# -*- coding: utf-8 -*-
"""법령 마스터 목록(창진원·중기부 리포트 JSON)과 디스크 XML 을 전수 대조한다.

파일 실재, 규범명 인덱스, 인용 조 존재, XML 무결성을 보고 `_verify_report.json` 으로 남긴다.
실행:  python scripts/archive/eval/verify_corpus.py
"""
from __future__ import annotations

# `scripts/_lib` 이 보일 때까지 위로 올라가 scripts/ 와 프로젝트 루트를 sys.path 맨 앞에 건다.
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
# archive 하위 폴더끼리 import 하므로 scripts/archive/ 의 모든 하위 폴더도 건다.
_archive_이관 = _os_이관.path.join(_p_이관, "archive")
if _os_이관.path.isdir(_archive_이관):
    for _d_이관 in _os_이관.listdir(_archive_이관):
        _full_이관 = _os_이관.path.join(_archive_이관, _d_이관)
        if _os_이관.path.isdir(_full_이관) and _full_이관 not in _sys_이관.path:
            _sys_이관.path.insert(0, _full_이관)


import glob
import json
import os
import sys
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))
import Law_Crawling as L  # noqa: E402

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "scripts" / "_lib").is_dir())
D = ROOT / "법령 PDF"


def load(name, default):
    p = D / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def disk_names() -> dict[str, str]:
    idx = {}
    for f in sorted(L.OUT_DIR.glob("*.xml")):
        r = ET.parse(f).getroot()
        nm = r.findtext(".//법령명_한글") or r.findtext(".//행정규칙명")
        if nm:
            idx.setdefault(L.norm(nm.strip()), f.name)
    return idx


def main() -> None:
    have = disk_names()
    problems = []
    print("디스크 규범 %d개 / 현행 XML %d개 / 연혁 %d개\n"
          % (len(have), len(list(L.OUT_DIR.glob("*.xml"))),
             len(list(L.HIST_DIR.glob("*.xml")))))

    for label, master, report, key in [
        ("창진원", None, load("_law_report.json", []), "keyword"),
        ("중기부", load("_mss_master.json", []), load("_mss_report.json", []), "name"),
    ]:
        print("=" * 62)
        print("[%s]" % label)
        rep_by = {}
        for r in report:
            rid = str(r.get("ref_id", ""))
            if not rid.startswith("D-"):
                rep_by[rid] = r

        if master is not None:
            # 마스터에 있는데 리포트에 없는 항목
            for m in master:
                rid = "M%02d" % m["no"]
                if rid not in rep_by:
                    problems.append((label, m["name"], "리포트에 항목 없음"))
        print("  마스터 항목 %d건 / 상태: %s"
              % (len(rep_by), dict(Counter(r.get("status") for r in rep_by.values()))))

        # 파일 실재
        gone = miss_hist = 0
        for r in rep_by.values():
            if r.get("status") not in ("수집",):
                continue
            for fo in r.get("files", []):
                p = (ROOT / fo["file"].replace("\\", "/"))
                if not p.exists():
                    if fo["kind"] == "시점본":
                        miss_hist += 1
                    else:
                        gone += 1
                    problems.append((label, r.get("name") or r.get("keyword"),
                                     "파일 없음: " + fo["file"]))
            # 규범명이 디스크 인덱스에 있는가
            if r.get("name") and L.norm(r["name"]) not in have:
                problems.append((label, r["name"], "디스크 규범 인덱스에 없음"))
        print("  현행 파일 누락 %d / 시점본 누락 %d" % (gone, miss_hist))

        bad_art = [(r.get("name"), a["ref"]) for r in rep_by.values()
                   for a in r.get("article_titles", []) if not a.get("ok")]
        print("  인용 조 검증 실패 %d" % len(bad_art))
        for nm, ref in bad_art[:8]:
            problems.append((label, nm, "인용 조 없음: " + ref))

        no_file = [r for r in rep_by.values()
                   if r.get("status") == "수집" and not r.get("files")]
        if no_file:
            print("  수집인데 파일 기록 없음 %d" % len(no_file))
            for r in no_file:
                problems.append((label, r.get("name"), "files 비어 있음"))

    # 위임 경로 파일 실재
    paths = load("_law_delegation_paths.json", {})
    gone = [v["file"] for v in paths.values()
            if v.get("file") and not (L.OUT_DIR / v["file"]).exists()]
    print("=" * 62)
    print("[위임 경로] %d개 규범 / 파일 누락 %d" % (len(paths), len(gone)))

    # 무결성
    allf = list(L.OUT_DIR.glob("*.xml")) + list(L.HIST_DIR.glob("*.xml"))
    bad = []
    for f in allf:
        try:
            r = ET.parse(f).getroot()
        except Exception:                                        # noqa: BLE001
            bad.append(f.name)
            continue
        # XML 본문 모양이 셋이다: 법령 .//조문단위 · 행정규칙 최상위 조문내용 · <조문> 래퍼 변종
        n = (len(r.findall(".//조문단위"))
             + len([c for c in r if c.tag == "조문내용"])
             + len(r.findall(".//조문/조문내용")))
        if n == 0:
            problems.append(("무결성", f.name, "조문 0개"))
    print("[무결성] XML %d개 / 파싱 실패 %d" % (len(allf), len(bad)))
    for b in bad:
        problems.append(("무결성", b, "파싱 실패"))

    print("\n" + "=" * 62)
    if not problems:
        print("문제 없음 — 마스터 대조 통과")
    else:
        print("발견된 문제 %d건" % len(problems))
        for src, nm, why in problems[:40]:
            print("  [%s] %-46s %s" % (src, str(nm)[:46], why))
    (D / "_verify_report.json").write_text(
        json.dumps([{"batch": a, "name": b, "issue": c} for a, b, c in problems],
                   ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
