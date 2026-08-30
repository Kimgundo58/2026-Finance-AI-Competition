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
# 🔴 키워드와 "을우선적용" 사이에 약칭 괄호·인용부호가 끼어든다 (2026-08-30 실측).
#    모두의창업 제4조①: 『…통합관리지침(이하 "지침" 이라 한다)」을 우선 적용하되…』
#    옛 패턴은 키워드 바로 뒤에 "을우선적용" 이 오기를 요구해 **이 한 건을 통째로 놓쳤다.**
#    그 결과 _precedence_rules.json 에 모두의창업의 L1>L2 가 빠져 있었다 — 문서 표기가 아니라
#    데이터 결손이고, 빠지면 지침보다 관리기준이 이기는 것으로 뒤집혀 판정 방향이 반대가 된다.
#    꼬리 문형(명시되지않 … 본 기준을 따른다)이 충분히 특정적이라 머리 쪽 간극은 열어둔다.
P_L1 = re.compile(
    r"(?:요령및지침|운영요령|통합관리지침|통합지침|지침).{0,60}?을?우선적용(?:하되|하고)"
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
            # 🔴 max_pages=8 이었다. 적용범위 조가 앞에만 있다는 전제였는데 틀렸다 —
            #    모두의창업은 제2편(일반·기술트랙)/제3편(로컬트랙) 구조라
            #    **제53조에 로컬트랙 전용 적용범위**가 따로 있고, 그게 통째로 누락됐다.
            #    로컬트랙의 상위 규범은 통합관리지침이 아니라 「신사업창업사관학교 운영지침」이다.
            t, _ = pdftext.extract(f)
            return t, str(f.relative_to(ROOT))
    return "", ""


# 사업 안에서 우리가 다루지 않는 구간. 여기서 잘라내지 않으면 **범위 밖 룰이
# 판정 경로로 들어온다** — precedence 조회 키가 `사업명` 이라 같은 사업의 다른 트랙
# 룰이 함께 걸린다.
#   모두의 창업 프로젝트: 제1편 총칙 / 제2편 일반·기술트랙 / 제3편 로컬트랙
#   우리 범위는 **일반·기술트랙**이다 (데이터셋 폴더도 `모두의 창업 (일반-기술)`).
#   로컬트랙은 상위 규범이 통합관리지침이 아니라 「신사업창업사관학교 운영지침」이라
#   계통 자체가 다르다.
# 목차에도 같은 문자열이 있으므로 **마지막 매치**를 쓴다. 목차 표기는 `제3편 로컬트랙`,
# 본문 헤딩은 `제 3 편 로컬트랙` 으로 자간이 벌어진다.
범위밖_구간 = {
    "모두의 창업 프로젝트": re.compile(r"(?:^|\n)\s*제\s*3\s*편\s*로컬트랙"),
}


def 스코프_컷(biz: str, raw: str) -> str:
    """범위 밖 편(篇)을 잘라낸다. 해당 사업이 없으면 원문 그대로."""
    pat = 범위밖_구간.get(biz)
    if not pat:
        return raw
    ms = list(pat.finditer(raw))
    return raw[:ms[-1].start()] if ms else raw


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
        raw = 스코프_컷(biz, raw)
        t = norm(raw)
        sq, imap = squash(t)

        m3s = list(P_L3.finditer(sq))
        m1s = list(P_L1.finditer(sq))
        m3 = m3s[0] if m3s else None
        m1 = m1s[0] if m1s else None
        if not (m3s or m1s):
            misses.append((biz, path, "우선순위 문형 미발견"))
            continue

        # 조번호 회수 — 매치 앞쪽(공백제거 기준)에서 가장 가까운 "제N조(...)"
        def 조번호(pos: int) -> str:
            head = sq[max(0, pos - 300):pos]
            hits = re.findall(r"제(\d+)조\(([^)]{2,20})\)", head)
            return f"제{hits[-1][0]}조({hits[-1][1]})" if hits else ""

        def 우선규범(sq: str, pos: int) -> str | None:
            """어느 규범이 우선하는가. 「…」 안의 이름을 앞에서 되짚는다.

            L1>L2 를 뭉뚱그리면 안 된다 — 모두의창업 로컬트랙의 상위는
            통합관리지침이 아니라 「신사업창업사관학교 운영지침」이다.
            """
            head = sq[max(0, pos - 120):pos + 40]
            names = re.findall(r"[「『]([^」』]{4,45})[」』]", head)
            return names[-1] if names else None

        def 원문(m) -> str:
            """공백제거 매치를 원문 구간으로 되돌린다."""
            a, b = imap[m.start()], imap[m.end() - 1] + 1
            return norm(t[a:b])

        # 한 문서에 적용범위 조가 여럿일 수 있다 (편/트랙 구조). 전수로 낸다.
        for m in m3s:
            rules.append({
                "사업명": biz, "우선계층": "L2", "열위계층": "L3", "범위": "all",
                "근거": [{"doc": path, "조번호": 조번호(m.start())}],
                "원문": 원문(m),
                "해석": "주관기관 규정이 더 엄격해도 세부관리기준이 이긴다",
                "verified": False,
            })
        for m in m1s:
            상위 = 우선규범(sq, m.start())
            rules.append({
                "사업명": biz, "우선계층": "L1", "열위계층": "L2", "범위": "unspecified_only",
                "우선규범": 상위,
                "근거": [{"doc": path, "조번호": 조번호(m.start())}],
                "원문": 원문(m),
                "해석": (f"{상위 or '지침·운영요령'} 이 우선. 거기 없거나 사업 특성상 "
                        "달리 정한 것만 이 관리기준"),
                "verified": False,
            })

    doc = {
        "생성": "scripts/build_precedence.py",
        "기준일": "2026-08-27",
        "주의": ("verified=false 다. 사업마다 문구가 달라 사람 검수가 필요하다 "
                 "(룰테이블_검수가이드.md 와 같은 취급)."),
        "핵심발견": ("8개 사업 중 6개가 'L2 > L3' 를 명시한다. 이는 예외가 아니라 규칙이다. "
                     "초격차·모두의창업 2개는 여기에 더해 'L1 > L2 (미규정 사항만 L2)' 를 명시한다. "
                     "초격차에는 L2 > L3 조항이 없고 L1 > L2 만 있다. "
                     "TIPS 는 R&D 라 주관기관이 아니라 운영사 체계여서 어느 조항도 없다."),
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
