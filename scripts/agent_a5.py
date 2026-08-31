# -*- coding: utf-8 -*-
"""A5 답변 축적 — 기관 공식 답변 → 룰 후보 → 검수 큐 (`Agent.md` §9 우선순위 3).

무엇을 푸는가
  사용자가 판단불가를 받고 기관에 문의하면(화면 9) 답이 온다. 그 답은 **규정에 없는
  운영 관행**을 담고 있어 다음 사람에게 값이 크다. 그런데 답변은 규범이 아니다 —
  담당자 개인 견해일 수도 있고, 기관마다 다르고, 우리가 검증하지 않았다.

🔴 **그래서 answer 를 rules 에 바로 넣지 않는다.** 후보로 만들어 검수 큐에 쌓는다.
   검수를 거치지 않은 답변으로 "가능" 을 내보내는 순간 우리는 근거 없는 확답을 판 것이 된다.
   오답 비대칭 그대로다 — 틀린 "가능" 보다 안전한 "조건부" 가 낫다.

🔴 **`corpus.rules` 를 UPDATE 하지 않는다** (계약서 §3, G 세션과 충돌).

입력 형식 (`--in`)
  [{"기관명": "...", "사업명": "...", "비목": "...",
    "질문": "...", "답변": "...", "수신일": "2026-08-20",
    "출처": "이메일|공문|전화메모", "담당부서": "..."}]

  🔴 **담당자 실명·연락처는 넣지 않는다.** 저장 계층화 원칙(`서비스 아키텍쳐.md` §6)에서
     F4 인력 이름조차 저장하지 않는다. 기관 담당자도 같다 — 부서까지만 남긴다.

무엇을 뽑는가
  답변 문장에서 허용(가능·조건부·불가) · 사전승인 · 한도 · 증빙을 **표현 그대로** 뽑는다.
  ⚠️ 여기서 LLM 을 쓰지 않는다. 규칙 추출이라 재현 가능하고, 애매하면 뽑지 않는다 —
     "확실한 패턴만 채우고 나머지는 NULL 로 남긴다" 가 이 프로젝트의 규칙이다.

실행:
    PYTHONIOENCODING=utf-8 python scripts/agent_a5.py --in scripts/_work/_A5_기관답변_픽스처.json --dry
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from agent_a2 import _금지, _사전승인, 금액들, 별칭표, 비목추정          # noqa: E402
from agent_a4 import 적재, 접기                                        # noqa: E402

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")
기본입력 = ROOT / "scripts" / "_work" / "_A5_기관답변_픽스처.json"

_가능 = re.compile(r"가능합니다|집행할\s*수\s*있|인정됩니다|사용하실\s*수\s*있|무방합니다")
_조건 = re.compile(r"경우에\s*한|한하여|조건으로|하는\s*경우에만|다만|단,")
# 증빙 이름만 집는다. 앞의 서술어("승인을 받아야 하며 …")까지 먹지 않도록
# **문서명 접미사로 끝나는 짧은 명사구**만 노린다. 애매하면 안 뽑는 쪽이 낫다
_증빙 = re.compile(r"[가-힣A-Za-z]{1,8}(?:\s[가-힣A-Za-z]{1,8})?"
                   r"(?:계산서|보고서|확인서|명세서|내역서|계약서|영수증|증명서|신청서|사용내역)")
_증빙제외 = re.compile(r"받아|하며|또는|그리고|제출|첨부|구비|징구")
# 실명·연락처가 섞여 들어오면 그 자리에서 지운다. 저장 계층화 원칙(§6).
_개인정보 = re.compile(r"(01[016-9][-\s]?\d{3,4}[-\s]?\d{4})|"
                      r"([\w.+-]+@[\w-]+\.[\w.]+)|"
                      r"(0\d{1,2}[-\s]?\d{3,4}[-\s]?\d{4})")


def 씻기(t: str) -> str:
    return _개인정보.sub("[삭제]", t or "")


def 후보추출(답변: str) -> dict:
    """답변 문장 → 룰 후보 조각. 확신 없는 건 None 으로 남긴다."""
    금지 = bool(_금지.search(답변))
    가능 = bool(_가능.search(답변))
    조건 = bool(_조건.search(답변))
    if 금지 and not 가능:
        허용 = "불가"
    elif 가능 and (조건 or _사전승인.search(답변)):
        허용 = "조건부"
    elif 가능:
        허용 = "가능"
    else:
        허용 = None                     # 🔴 못 읽으면 비운다. 추측하지 않는다
    금액 = 금액들(답변)
    return {
        "허용": 허용,
        "사전승인": bool(_사전승인.search(답변)) or None,
        "한도_값": max(금액) if 금액 and 허용 in ("가능", "조건부") else None,
        "증빙": sorted({m.strip() for m in _증빙.findall(답변)
                        if not _증빙제외.search(m)}) or None,
    }


def 충돌(cur, 사업명: str | None, 비목: str | None, 후보: dict) -> dict:
    """기존 룰과 어긋나는가. 어긋나면 그게 큐의 값이다."""
    if not (사업명 and 비목):
        return {"기존룰": None, "판단": "사업·비목 미확정 — 사람이 붙여야 한다"}
    행 = cur.execute("""
        SELECT rule_id, "허용", "사전승인", "한도_값", "verified" FROM corpus.rules
        WHERE "사업명" = %s AND "비목" = %s AND "layer" IN ('L1','L2')
        ORDER BY ("layer"='L2') DESC, rule_id LIMIT 1
    """, (사업명, 비목)).fetchone()
    if not 행:
        return {"기존룰": None, "판단": "해당 사업·비목에 룰이 없다 — 신규 후보"}
    rid, 허용, 사전, 한도, ver = 행
    다름 = []
    if 후보["허용"] and 후보["허용"] != 허용:
        다름.append(f"허용 {허용} → 답변은 {후보['허용']}")
    if 후보["사전승인"] and not 사전:
        다름.append("답변은 사전승인을 요구하는데 룰에는 없다")
    if 후보["한도_값"] and 한도 is not None and float(후보["한도_값"]) != float(한도):
        다름.append(f"한도 {한도} → 답변은 {후보['한도_값']}")
    return {"기존룰": {"rule_id": rid, "허용": 허용, "verified": ver},
            "판단": " · ".join(다름) if 다름 else "기존 룰과 어긋나지 않는다 (확인 값)"}


def main() -> None:
    ap = argparse.ArgumentParser(description="A5 기관 답변 → 룰 후보 → 검수 큐")
    ap.add_argument("--in", dest="입력", default=str(기본입력))
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    경로 = Path(a.입력)
    if not 경로.exists():
        sys.exit(f"입력이 없다: {경로}\n"
                 f"형식은 이 파일 docstring 참조. 실데이터가 아직 0건이라 픽스처로 배관만 검증한다.")
    답변들 = json.loads(경로.read_text(encoding="utf-8"))

    print(f"A5 답변 축적 — {경로.name} · {len(답변들)}건")
    recs: list[dict] = []
    with psycopg.connect(DSN) as conn:
        cur = conn.cursor()
        별칭 = 별칭표(cur)
        for d in 답변들:
            답변 = 씻기(d.get("답변", ""))
            질문 = 씻기(d.get("질문", ""))
            비목 = d.get("비목") or 비목추정(질문, 답변, 별칭)
            사업명 = d.get("사업명")
            후보 = 후보추출(답변)
            c = 충돌(cur, 사업명, 비목, 후보)
            print(f"\n  [{d.get('기관명','?')}] {사업명 or '?'} / {비목 or '비목미확정'}")
            print(f"    후보: {후보}")
            print(f"    대조: {c['판단']}")
            recs.append({
                "종류": "A5기관답변", "사유코드": "ORG_ANSWER",
                "대상종류": "rule" if (c["기존룰"]) else "none",
                "대상ID": str(c["기존룰"]["rule_id"]) if c["기존룰"] else None,
                "사업명": 사업명, "비목": 비목,
                # 답변에는 규범 좌표가 없다. doc_id 자리에 기관명을 넣지 않는다 —
                # 그 컬럼은 규범 문서 식별자이고 섞으면 나중에 조인이 깨진다
                "doc_id": None, "조번호": None, "구doc_id": None, "구조번호": None,
                "변경유형": None, "유사도": None,
                "요약": f"[{d.get('기관명','?')}] {d.get('수신일','')} 답변 — "
                        f"후보 허용={후보['허용'] or '미상'} · {c['판단']}",
                "상세": {"기관명": d.get("기관명"), "담당부서": d.get("담당부서"),
                        "출처": d.get("출처"), "수신일": d.get("수신일"),
                        "질문": 질문[:400], "답변": 답변[:800],
                        "룰후보": 후보, "대조": c,
                        "주의": "검수 전 답변이다. 이대로 rules 에 넣지 말 것"},
            })
        recs = 접기(recs)
        n, msg = 적재(conn, recs, a.dry)
        print(f"\n적재: {msg}")
        print("🔴 이 큐 항목은 사람이 검수해 `verified=false` 룰로 올린다. "
              "답변만으로 '가능' 이 나가면 안 된다.")


if __name__ == "__main__":
    main()
