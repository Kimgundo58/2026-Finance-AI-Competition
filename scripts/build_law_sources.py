# -*- coding: utf-8 -*-
"""규범별 출처(배치) 통합 인덱스 생성.

법령 파일은 규범당 한 벌만 둔다. 어느 배치가 그 규범을 참조하는지는 파일이 아니라
이 인덱스로 구분한다. 창진원(L2)과 중기부(L1)가 같은 법을 함께 인용하는 경우가
많기 때문에 `sources` 는 **단일 값이 아니라 배열**이어야 한다.

산출: `법령 PDF/_law_sources.json`
  { 정규화규범명: {file, name, sources:[...], refs:{배치:[ref_id]},
                   cited:{배치:[조]}, delegation:{...}} }

실행: python scripts/build_law_sources.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))
import Law_Crawling as L  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
D = ROOT / "법령 PDF"
OUT = D / "_law_sources.json"


def load(p: Path, default):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def disk_index() -> dict[str, dict]:
    idx = {}
    for f in sorted(L.OUT_DIR.glob("*.xml")):
        root = ET.parse(f).getroot()
        nm = root.findtext(".//법령명_한글") or root.findtext(".//행정규칙명")
        if not nm:
            continue
        nm = nm.strip()
        k = L.norm(nm)
        ministry = (root.findtext(".//소관부처") or root.findtext(".//소관부처명") or "").strip()
        idx.setdefault(k, {"name": nm, "file": f.name,
                           "norm_type": "행정규칙" if root.findtext(".//행정규칙명") else "법령",
                           "ministry": ministry,
                           "sources": [], "refs": {}, "cited": {}})
        # 중복 파일이 있으면 파일명이 짧은(정규) 쪽을 대표로
        if len(f.name) < len(idx[k]["file"]):
            idx[k]["file"] = f.name
    return idx


def add(idx, key, source, ref_id, cited):
    e = idx.get(key)
    if e is None:
        return False
    if source not in e["sources"]:
        e["sources"].append(source)
    e["refs"].setdefault(source, [])
    if ref_id and ref_id not in e["refs"][source]:
        e["refs"][source].append(ref_id)
    if cited:
        cur = e["cited"].setdefault(source, [])
        for c in cited:
            if c not in cur:
                cur.append(c)
    return True


# ── 판정 인덱스 편입 여부 ──────────────────────────────────────
# 위임 추적은 근거 사슬을 완성하려고 넓게 따라가지만, 그렇게 딸려온 것 중에는
# 창업지원금 판정에 절대 쓰이면 안 되는 규범이 섞인다. 대표적으로 보조금법
# 제26조의2가 각 부처 관리규정에 위임해서 들어온 **타부처 국고보조금 규정**이다.
# 「교육부 국고보조사업 관리규정」이 창업지원금 판정 근거로 인용되면 그게 최악의 사고다.
# 파일은 보관하되(사슬 완성용) 판정 인덱스에서는 뺀다.
OUR_MINISTRY = ("중소벤처기업부", "중소기업청", "기획재정부")

_RE_SUBSIDY = re.compile(r"(보조금|보조사업|국고보조|민간보조|민간위탁)")
_RE_WELFARE = re.compile(r"(아이돌봄|기초연금|국민기초생활|장애인복지|한부모가족|"
                         r"영유아보육|사회복지사업|사회서비스 이용|사회서비스이용)")
_RE_RND_ORG = re.compile(r"(국가연구개발사업|연구개발사업).*(전문기관 지정|연구관리 전문기관)")


def apply_index_flags(idx: dict) -> None:
    for e in idx.values():
        nm, mi = e["name"], e.get("ministry", "")
        # 문서가 직접 인용한 것은 무조건 편입 (보조금법은 기재부 소관이지만 직접참조다).
        # 단 위임으로 딸려온 것(ref_id 가 'D-')은 직접참조가 아니다 — 이걸 섞으면
        # 타부처 보조금 규정이 전부 직접참조로 잡혀 필터가 무력화된다.
        direct = any(r for s in ("창진원", "중기부") for r in e["refs"].get(s, [])
                     if not str(r).startswith("D-"))
        if direct:
            e["index"] = True
            e["index_reason"] = "직접참조"
            continue
        if _RE_WELFARE.search(nm):
            e["index"] = False
            e["index_reason"] = "복지·수당 계열 — 보조금법 압류금지·정보연계 조항 경유"
        elif _RE_SUBSIDY.search(nm) and not mi.startswith(OUR_MINISTRY):
            e["index"] = False
            e["index_reason"] = "타부처 보조금 규정(%s) — 창업지원금 판정 근거 아님" % (mi or "소관 미상")
        elif _RE_RND_ORG.search(nm) and not mi.startswith(OUR_MINISTRY):
            e["index"] = False
            e["index_reason"] = "타부처 R&D 전문기관 지정 고시(%s)" % (mi or "소관 미상")
        elif "범위밖" in e["sources"]:
            e["index"] = False
            e["index_reason"] = "위임 아님 — 인용법령 경유"
        else:
            e["index"] = True
            e["index_reason"] = "위임 사슬"


def main() -> None:
    idx = disk_index()
    miss = []

    # 창진원 배치 — 직접 참조
    for r in load(D / "_law_report.json", []):
        if r.get("status") not in ("수집",) or not r.get("name"):
            continue
        if not add(idx, L.norm(r["name"]), "창진원", r.get("ref_id"), r.get("cited")):
            miss.append(("창진원", r["name"]))

    # 창진원 배치 — 위임으로 딸려온 것
    paths = load(D / "_law_delegation_paths.json", {})
    for k, v in paths.items():
        if v.get("kind") == "범위밖":
            e = idx.get(k)
            if e is not None and "범위밖" not in e["sources"]:
                e["sources"].append("범위밖")
                e["note"] = v.get("note")
            continue
        if add(idx, k, "창진원", None, None):
            idx[k]["delegation"] = {"from": v.get("delegated_from"),
                                    "via": v.get("delegate_via"),
                                    "kind": v.get("kind"), "depth": v.get("depth"),
                                    "chain": v.get("chain")}

    # 중기부 배치 — 직접 참조 + 위임
    for r in load(D / "_mss_report.json", []):
        if r.get("status") != "수집" or not r.get("name"):
            continue
        k = L.norm(r["name"])
        if not add(idx, k, "중기부", r.get("ref_id"), r.get("cited")):
            miss.append(("중기부", r["name"]))
            continue
        if r.get("delegated_from"):
            idx[k].setdefault("delegation", {
                "from": r.get("delegated_from"), "via": r.get("delegate_via"),
                "kind": r.get("delegate_kind"), "depth": r.get("depth")})

    for e in idx.values():
        e["sources"] = sorted(e["sources"])
    apply_index_flags(idx)

    OUT.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")

    from collections import Counter
    c = Counter(",".join(e["sources"]) or "(출처없음)" for e in idx.values())
    print("규범 %d개 → %s" % (len(idx), OUT.name))
    for k, v in sorted(c.items(), key=lambda x: -x[1]):
        print("  %-18s %d" % (k, v))
    if miss:
        print("\n디스크에 없는 참조 %d건:" % len(miss))
        for s, n in miss[:10]:
            print("   [%s] %s" % (s, n))


if __name__ == "__main__":
    main()
