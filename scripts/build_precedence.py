# -*- coding: utf-8 -*-
"""우선순위 조항 추출 → `_precedence_rules.json`.

「어느 계층이 이기는가」를 각 세부관리기준의 제3조(적용범위) 부근에서 뽑는다.
비목 룰이 아니라 **충돌 해소 룰**이다. 상세: rule_base.md §3

왜 필요한가
  프로젝트설명 §2-3 과 CLAUDE.md 가 "충돌 해소는 '아래가 엄격하면 이긴다'가 아니다"
  라고 하면서 재도전성공패키지 제3조를 **예외 사례**로 들었다. 실측해보니 예외가 아니다 —
  **7개 사업 중 6개가 전부 "주관기관 내부규정과 상충되는 경우 본 관리기준 우선"** 이다.
  즉 L2 > L3 가 규칙이고, L3 가 더 엄격해도 진다.

  TIPS 만 해당 조항이 없다. R&D 사업이라 주관기관이 아니라 운영사(민간) 체계이고,
  '우선 적용'이 문서 내부 장(章) 간 우선순위를 뜻한다.

실행:
    python scripts/build_precedence.py            추출 + 저장
    python scripts/build_precedence.py --show     결과만 출력
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "2026_Finance_DATA_FOR_RAG" / "_precedence_rules.json"

# 적용범위 조항이 실린 판본. 변환본(_hwp변환) 우선, 없으면 원본 PDF.
SOURCES = {
    "예비창업패키지":        "창진원/예비창업패키지/예비창업패키지 세부관리기준(2025년)",
    "초기창업패키지":        "창진원/초기창업패키지/초기창업패키지 세부관리기준(2025년)",
    "재도전성공패키지":      "창진원/재도전성공패키지/재도전성공패키지 세부관리기준(2025년)",
    "창업도약패키지":        "창진원/창업도약패키지/창업도약패키지 세부관리기준(2025년)",
    "창업중심대학":          "창진원/창업중심대학/창업중심대학 세부관리기준2025년 개정",
    "초격차 스타트업 프로젝트": "창진원/초격차 스타트업 프로젝트/초격차 스타트업 프로젝트 세부관리기준(제10차)",
    "모두의 창업 프로젝트":   "창진원/모두의 창업 (일반-기술)/모두의 창업 프로젝트 세부관리기준(개정본)",
    "민관공동창업자발굴육성(TIPS)": "창진원/민관공동창업자발굴육성(TIPS)/2026/첨부 붙임1. 2026년 팁스TIPS 총괄 운영지침 1차 개정안 본문",
}

# 두 갈래의 문형.
# ⚠️ 다단 레이아웃 PDF 는 단어 **중간**에 줄바꿈이 들어간다 (예: "창" + 개행 + "업중심대학").
#    그래서 공백을 전부 제거한 사본에서 매칭하고, 인덱스 맵으로 원문을 회수한다.
# 주체("주관기관의"/"창업중심대학의")는 앵커에 넣지 않는다 — 다단 레이아웃에서
# 다른 컬럼 텍스트가 단어 **사이**로 끼어드는 사례가 실측됐다
# (창업중심대학: "창" + 다른컬럼 한 문장 + "업중심대학의"). 꼬리만 붙잡는다.
P_L3 = re.compile(r"내부규정과상충되는경우본관리기준을?우선(?:하여적용)?한다")
# "우선 적용하되"(예비·모두의창업)와 "우선 적용하고"(초격차) 둘 다 쓰인다.
P_L1 = re.compile(
    r"(?:요령및지침|운영요령|통합관리지침|통합지침|지침)을?우선적용(?:하되|하고)"
    r"[^。]{0,200}?(?:명시되지않|정하지아니한)[^。]{0,220}?본(?:관리기준|기준)을?(?:따른다|적용한다)")


def load_text(stem: str) -> tuple[str, str]:
    """변환본 → 원본 순으로 찾아 텍스트를 반환. (본문, 실제경로)"""
    for base in (ROOT / "_hwp변환" / "2026_Finance_DATA_FOR_RAG",
                 ROOT / "2026_Finance_DATA_FOR_RAG"):
        for ext in (".pdf", ".txt"):
            f = base / (stem + ext)
            if not f.exists():
                continue
            if ext == ".txt":
                return f.read_text(encoding="utf-8", errors="replace"), str(f.relative_to(ROOT))
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import pdftext
            # 문자중복 레이어 자동 해소 (창업도약 2022년판 등이 해당)
            t, _ = pdftext.extract(f, max_pages=8)
            return t, str(f.relative_to(ROOT))
    return "", ""


def norm(t: str) -> str:
    return re.sub(r"\s+", " ", t)


def squash(t: str) -> tuple[str, list[int]]:
    """공백을 전부 없앤 사본과, 그 각 문자가 원문 몇 번째였는지의 맵."""
    buf, idx = [], []
    for i, ch in enumerate(t):
        if not ch.isspace():
            buf.append(ch)
            idx.append(i)
    return "".join(buf), idx


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    rules, misses = [], []
    for biz, stem in SOURCES.items():
        raw, path = load_text(stem)
        if not raw:
            misses.append((biz, stem, "파일 없음"))
            continue
        t = norm(raw)
        sq, imap = squash(t)

        m3 = P_L3.search(sq)
        m1 = P_L1.search(sq)
        if not (m3 or m1):
            misses.append((biz, path, "우선순위 문형 미발견"))
            continue

        # 조번호 회수 — 매치 앞쪽(공백제거 기준)에서 가장 가까운 "제N조(...)"
        def 조번호(pos: int) -> str:
            head = sq[max(0, pos - 300):pos]
            hits = re.findall(r"제(\d+)조\(([^)]{2,20})\)", head)
            return f"제{hits[-1][0]}조({hits[-1][1]})" if hits else ""

        def 원문(m) -> str:
            """공백제거 매치를 원문 구간으로 되돌린다."""
            a, b = imap[m.start()], imap[m.end() - 1] + 1
            return norm(t[a:b])

        if m3:
            rules.append({
                "사업명": biz, "우선계층": "L2", "열위계층": "L3", "범위": "all",
                "근거": [{"doc": path, "조번호": 조번호(m3.start())}],
                "원문": 원문(m3),
                "해석": "주관기관 규정이 더 엄격해도 세부관리기준이 이긴다",
                "verified": False,
            })
        if m1:
            rules.append({
                "사업명": biz, "우선계층": "L1", "열위계층": "L2", "범위": "unspecified_only",
                "근거": [{"doc": path, "조번호": 조번호(m1.start())}],
                "원문": 원문(m1),
                "해석": "지침·운영요령이 우선. 지침에 없거나 사업 특성상 달리 정한 것만 L2",
                "verified": False,
            })

    doc = {
        "생성": "scripts/build_precedence.py",
        "기준일": "2026-08-27",
        "주의": ("verified=false 다. 사업마다 문구가 달라 사람 검수가 필요하다 "
                 "(룰테이블_검수가이드.md 와 같은 취급)."),
        "핵심발견": ("7개 사업 중 6개가 'L2 > L3' 를 명시한다. 이는 예외가 아니라 규칙이다. "
                     "TIPS 는 R&D 라 주관기관이 아니라 운영사 체계여서 해당 조항이 없다."),
        "rules": rules,
        "미발견": [{"사업명": b, "경로": p, "사유": r} for b, p, r in misses],
    }
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"우선순위 룰 {len(rules)}건 → {OUT.relative_to(ROOT)}")
    for r in rules:
        print(f"  · {r['사업명']:24s} {r['우선계층']} > {r['열위계층']:3s} "
              f"[{r['범위']}]  {r['근거'][0]['조번호']}")
    for b, p, why in misses:
        print(f"  ✗ {b:24s} {why}")


if __name__ == "__main__":
    main()
