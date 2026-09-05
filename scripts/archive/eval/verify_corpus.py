# -*- coding: utf-8 -*-
"""마스터 문서 ↔ 디스크 전수 대조.

창진원(`_law_report.json`)·중기부(`_mss_master.json` + `_mss_report.json`) 두 배치의
마스터 목록을 기준으로, 각 항목이 실제로 디스크에 있는지 확인한다. 파일 존재만
보지 않고 **인용된 조가 그 법에 실재하는지**(`article_titles`)까지 본다 — 창진원
배치에서 이 검증이 「산업교육진흥법 시행규칙」 누락을 잡아냈다.

실행: python scripts/archive/eval/verify_corpus.py
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


import glob
import json
import os
import sys
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))
import Law_Crawling as L  # noqa: E402

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "scripts" / "_lib").is_dir())  # 🔴 2026-09-05 archive 이관 — 깊이 무관 계산으로 교체
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
        # 행정규칙 XML 은 본문 모양이 셋이다:
        #   (a) 법령        → .//조문단위
        #   (b) 행정규칙     → 최상위 조문내용 반복
        #   (c) 행정규칙 변종 → <조문> 래퍼 안에 조문번호/제목/내용 삼중주
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
