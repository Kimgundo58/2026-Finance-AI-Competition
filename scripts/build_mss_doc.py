# -*- coding: utf-8 -*-
"""중기부(L1) 참조 규범 마스터 문서 생성.

`_mss_master.json`(문서에서 추출한 84건) + `_mss_report.json`(실제 수집 결과)을
대조해 `중기부_법령_링크모음.md` 를 만든다. 수집 상태를 마스터와 나란히 두는 게
목적이라 — 빠진 게 있으면 표에서 바로 보인다.

실행: python scripts/build_mss_doc.py
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MASTER = ROOT / "법령 PDF" / "_mss_master.json"
REPORT = ROOT / "법령 PDF" / "_mss_report.json"
OUT = ROOT / "중기부_법령_링크모음.md"

SECTIONS = [
    (1, 5, "2-1. 창업지원사업 직접 규율 핵심 법률"),
    (6, 21, "2-2. 사업비 집행·제재 관련 법률"),
    (22, 31, "2-3. 계약·조달 관련 법률 — 보조사업 관리규정 인용"),
    (32, 59, "2-4. 보조금법 원문이 내부 인용하는 법률"),
    (60, 62, "2-5. 공고문·신청양식 경유 법률"),
    (63, 70, "2-6. 대통령령·부령"),
    (71, 84, "2-7. 행정규칙·지침·공고류"),
]

MARK = {"수집": "✅", "미등재": "❌", "미확인": "⚠️", "확인": "·"}


def fmt_jo(c: str) -> str:
    """'26-2' → '제26조의2' / '35' → '제35조'."""
    if "-" in c:
        a, b = c.split("-", 1)
        return "제%s조의%s" % (a, b)
    return "제%s조" % c


def link(name: str) -> str:
    base = "행정규칙" if name.endswith(("지침", "요령", "규정", "기준", "고시")) else "법령"
    return "https://www.law.go.kr/%s/%s" % (base, name.replace(" ", ""))


def main() -> None:
    master = json.loads(MASTER.read_text(encoding="utf-8"))
    rep = json.loads(REPORT.read_text(encoding="utf-8")) if REPORT.exists() else []
    by_no = {}
    for r in rep:
        rid = r.get("ref_id", "")
        if rid.startswith("M") and rid[1:].isdigit():
            by_no[int(rid[1:])] = r
    deleg = [r for r in rep if str(r.get("ref_id", "")).startswith("D-")]

    n_ok = sum(1 for r in by_no.values() if r["status"] == "수집")
    n_no = sum(1 for r in by_no.values() if r["status"] == "미등재")
    n_dup = sum(1 for r in by_no.values() if str(r.get("dedup", "")).startswith("기보유"))

    L = []
    L.append("# 중기부 법령 링크모음 (L1)")
    L.append("")
    L.append("「써도돼요」 **L1 = 중소벤처기업부(총괄기관)** 문서가 참조하는 법령·행정규칙 마스터.")
    L.append("창진원(L2) 배치는 `법령 연계 모음/문서_법령_링크모음.md` 를 본다.")
    L.append("")
    L.append("- 원천 문서: 통합관리지침 제14차·제13차, 중소기업창업 지원사업 운영요령,")
    L.append("  중소벤처기업부 보조사업 관리규정, 보조금 관리에 관한 법률 원문,")
    L.append("  2026년 창업지원사업 통합공고문, 부정행위 방지 사례집, 주관기관 매뉴얼, 신청양식")
    L.append("- 이 문서는 `scripts/build_mss_doc.py` 가 `_mss_master.json` + `_mss_report.json` 에서 생성한다. **직접 수정하지 말 것.**")
    L.append("")
    L.append("## 1. 수집 현황")
    L.append("")
    L.append("| 항목 | 건수 |")
    L.append("|---|---|")
    L.append("| 마스터 총계 | **%d** |" % len(master))
    L.append("| 수집 완료 | %d |" % n_ok)
    L.append("| 법령정보센터 미등재 | %d |" % n_no)
    L.append("| 창진원 배치와 공유(파일 1벌) | %d |" % n_dup)
    L.append("| 위임 추적으로 추가 수집 | %d |" % len(deleg))
    L.append("")
    L.append("> **출처 메타데이터**: 창진원 배치와 같은 폴더(`법령 PDF/L1_법령/`)에 저장하되")
    L.append("> 파일은 규범당 한 벌만 둔다. 어느 배치가 참조하는지는 `_law_sources.json` 의")
    L.append("> `sources` 배열로 구분한다 (`[\"창진원\"]` / `[\"중기부\"]` / 둘 다).")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 2. 마스터 목록")
    L.append("")
    L.append("범례: ✅ 수집 · ❌ 미등재 · ⚠️ 확인 필요")
    L.append("")

    for lo, hi, title in SECTIONS:
        rows = [m for m in master if lo <= m["no"] <= hi]
        L.append("### %s" % title)
        L.append("")
        L.append("| # | 정식 명칭 | 종류 | 상태 | 시행일 | 인용 조항 | 등장 문서 | 비고 |")
        L.append("|---|---|---|---|---|---|---|---|")
        for m in rows:
            r = by_no.get(m["no"], {})
            st = r.get("status", "미확인")
            mark = MARK.get(st, "·")
            eff = r.get("effective_date", "—")
            dup = " 🔗" if str(r.get("dedup", "")).startswith("기보유") else ""
            cited = ", ".join(fmt_jo(c) for c in m["cited"]) or "—"
            note = m["note"] or ""
            if r.get("flags"):
                note = (note + " / " if note else "") + " · ".join(r["flags"])
            L.append("| %d | [%s](%s) | %s | %s %s%s | %s | %s | %s | %s |" % (
                m["no"], m["name"], link(m["name"]), m["doc_type"], mark, st, dup,
                eff, cited, m["docs"][:70], note[:110]))
        L.append("")

    L.append("---")
    L.append("")
    L.append("## 3. 미등재 — 별도 조달 필요")
    L.append("")
    L.append("| # | 명칭 | 상태 | 조달 경로 |")
    L.append("|---|---|---|---|")
    for m in master:
        r = by_no.get(m["no"], {})
        if r.get("status") != "미등재":
            continue
        route = {
            71: "**원문 PDF 보유** — `2026_Finance_DATA_FOR_RAG/중기부/L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223.pdf`",
            72: "**원문 PDF 보유** — `2026_Finance_DATA_FOR_RAG/중기부/L1_창업사업화_지원사업_통합관리지침_제13차개정_20250205.pdf`",
            73: "미확보 — K-Startup 과거 공고 아카이브 (공고 제2022-6호)",
            77: "미확보 — 기획재정부(moef.go.kr) / 열린재정",
            84: "미확보 — 정식 명칭 미확정. 중기부 훈령 목록에서 확인 필요",
        }.get(m["no"], "—")
        L.append("| %d | %s | 미등재 | %s |" % (m["no"], m["name"], route))
    L.append("")
    L.append("실질 미확보는 **3건**(#73·#77·#84)이다. #71·#72는 PDF 원문을 이미 갖고 있다.")
    L.append("")

    if deleg:
        L.append("## 4. 위임 추적으로 추가 수집된 규범 (%d건)" % len(deleg))
        L.append("")
        L.append("| 규범 | 종류 | 위임 출처 | 위임 조항 |")
        L.append("|---|---|---|---|")
        for d in sorted(deleg, key=lambda x: x.get("name") or ""):
            L.append("| %s | %s | %s | %s |" % (
                d.get("name"), d.get("delegate_kind", ""),
                d.get("delegated_from", ""), d.get("delegate_via", "") or "—"))
        L.append("")

    OUT.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("생성: %s (%d줄)" % (OUT.name, len(L)))


if __name__ == "__main__":
    main()
