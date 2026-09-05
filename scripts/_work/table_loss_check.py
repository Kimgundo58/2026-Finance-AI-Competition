# -*- coding: utf-8 -*-
"""전수 소실 검사기 — `_stage0_articles.json`(원문) ↔ `corpus.doc_articles`(현재 DB)
어절 다중집합 대조. 2026-09-05 사고(레인 C, ai-35 배정) C2 산출물.

**표 기호를 정규화하고 비교한다** — `text_coverage.부족_어절()` 이 `|` 를 걷고
순서를 안 본다(표는 프로즈를 컬럼으로 재배치하므로 순서 대조는 항상 오탐).
그래도 남는 두 가지 조판 잔재(쪽번호 순수 숫자, `[라벨]` 헤딩 반복)는 여기서
따로 걷는다 — `table_splice.py` 의 복구 필터(`RE_복구_노이즈`)와 같은 규칙.

계량 축(중앙 지시): 창업중심대학 참고1·2·4 는 09-05 재파싱 대상이 아니라
원문과 글자수까지 일치해야 한다 — 이 셋에 0건이 안 나오면 검사기 자체가
잘못 눈금 잡힌 것이다. `--calibrate` 로 이 셋만 돌려 확인한다.

실행:
    python scripts/_work/table_loss_check.py --calibrate     # 눈금 확인(0건 기대)
    python scripts/_work/table_loss_check.py --target        # 09-05 재적재 대상 8개 조
    python scripts/_work/table_loss_check.py --doc "창업중심대학 세부관리기준2025년 개정"
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from _lib import db, text_coverage  # noqa: E402

STAGE0_PATH = ROOT / "2026_Finance_DATA_FOR_RAG" / "_stage0_articles.json"

RE_노이즈 = re.compile(r"^(\d+|\[.*\])$")

# 09-05 재적재 대상 8개 조 (레인 C 배정 메시지 원문 그대로).
재적재_대상 = [
    ("창업중심대학 세부관리기준2025년 개정", "참고3"),
    ("창업중심대학 세부관리기준2025년 개정", "참고5"),
    ("초격차 스타트업 프로젝트 세부관리기준(제10차)", "참고3"),
    ("초격차 스타트업 프로젝트 세부관리기준(제10차)", "참고6"),
    ("붙임1. 2026년 팁스TIPS 총괄 운영지침 3차 개정안 본문", "별첨1"),
    ("붙임1. 2026년 팁스TIPS 총괄 운영지침 3차 개정안 본문", "붙임3"),
    ("붙임1. 2026년 팁스TIPS 총괄 운영지침 3차 개정안 본문", "붙임5"),
    ("창업도약패키지 세부관리기준(2025년)", "별지서식"),
]

# 계량 축 — 재적재 안 된 조. 이 셋은 0건이 나와야 검사기가 맞게 눈금 잡힌 것.
계량_대상 = [
    ("창업중심대학 세부관리기준2025년 개정", "참고1"),
    ("창업중심대학 세부관리기준2025년 개정", "참고2"),
    ("창업중심대학 세부관리기준2025년 개정", "참고4"),
]


def _stage0_본문(stage0: dict, doc_id: str, 조번호: str) -> str | None:
    doc = stage0.get(doc_id)
    if not doc:
        return None
    for a in doc.get("articles", []):
        if a.get("조번호") == 조번호:
            return a.get("본문") or ""
    return None


def 검사(pairs: list[tuple[str, str]]) -> int:
    stage0 = json.loads(STAGE0_PATH.read_text(encoding="utf-8"))
    이상 = 0
    with db.connect(autocommit=True) as conn, conn.cursor() as cur:
        for doc_id, 조번호 in pairs:
            원문 = _stage0_본문(stage0, doc_id, 조번호)
            if 원문 is None:
                print(f"?  {doc_id[:40]:<40} {조번호:<8}  stage0 에 없음 — 건너뜀")
                continue
            cur.execute(
                "SELECT 본문 FROM corpus.doc_articles WHERE doc_id=%s AND 조번호=%s",
                (doc_id, 조번호))
            row = cur.fetchone()
            if row is None:
                print(f"?  {doc_id[:40]:<40} {조번호:<8}  DB 에 없음 — 건너뜀")
                continue
            db_본문 = row[0] or ""
            부족 = [t for t in text_coverage.부족_어절(원문, db_본문) if not RE_노이즈.match(t)]
            상태 = "이상" if 부족 else "정상"
            if 부족:
                이상 += 1
            print(f"{상태}  {doc_id[:40]:<40} {조번호:<8}  "
                  f"원문 {len(원문):>6}자  DB {len(db_본문):>6}자  부족어절 {len(부족):>4}")
            if 부족:
                미리보기 = " ".join(부족[:30])
                print(f"      -> {미리보기}{' …' if len(부족) > 30 else ''}")
    return 이상


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--calibrate", action="store_true", help="계량 축(재적재 안 된 조) 만 — 0건 기대")
    g.add_argument("--target", action="store_true", help="09-05 재적재 대상 8개 조")
    g.add_argument("--doc", help="이 doc_id 의 모든 조 전수 검사")
    a = ap.parse_args()

    if a.doc:
        stage0 = json.loads(STAGE0_PATH.read_text(encoding="utf-8"))
        doc = stage0.get(a.doc)
        if not doc:
            sys.exit(f"stage0 에 doc_id 없음: {a.doc}")
        pairs = [(a.doc, art["조번호"]) for art in doc.get("articles", [])]
    elif a.calibrate:
        pairs = 계량_대상
    else:
        pairs = 재적재_대상

    이상 = 검사(pairs)
    print(f"\n총 {len(pairs)}건 중 이상 {이상}건")


if __name__ == "__main__":
    main()
