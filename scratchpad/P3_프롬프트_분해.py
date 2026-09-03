# -*- coding: utf-8 -*-
"""P3 측정 — 채점 93문항 dry 조립의 블록별 자수·S번호·표 블록 분해.

읽기 전용이다. `orchestrate.판정(dry=True, 기록=False)` 만 부르고 DB 에 한 행도 쓰지 않는다
(dry 는 LLM 호출 전에 멈추므로 tenant.unmapped_premise·tenant.decisions·tenant.incidents
경로에 도달하지 않는다 — orchestrate.py:619 · 전제해소 는 (5) 단계라 그 뒤다).

`조립()` 은 프롬프트 문자열만 돌려주고 블록별 자수를 남기지 않는다. 그래서 여기서
`orchestrate.조립` 을 감싸(spy) 호출 인자와 반환 프롬프트를 그대로 받아 쪼갠다.
🔴 orchestrate 가 `from assemble_context import 조립` 로 이름을 들여왔으므로
   패치 대상은 `orchestrate.조립` 이다. 원본 파일은 건드리지 않는다.

실행:
    PYTHONIOENCODING=utf-8 python scratchpad/P3_프롬프트_분해.py --out scratchpad/P3_분해_전.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

_여기 = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_여기), "scripts"))

from _lib import db          # noqa: E402
import eval_store            # noqa: E402
import assemble_context      # noqa: E402
import orchestrate           # noqa: E402

RE_S = re.compile(r"^\[(S\d+)\] ", re.M)
# 부속표(붙임·참고·별표·별지·서식)는 조가 아니라 표다 — 항분해()가 못 쪼개는 갈래
RE_부속 = re.compile(r"^\s*[\[<(]?\s*(붙임|참고|별표|별지|서식|첨부)")

# 🔴 블록 경계 정의는 `assemble_context` 하나뿐이다 — 여기서 정규식을 다시 들지 않는다.
#    P4 의 run 저장도 같은 함수를 부른다(2026-09-03 P3↔P4 합의).
블록분해 = assemble_context.블록자수


def S조각(블록: str) -> list[tuple[str, int]]:
    """블록 안의 [Sxx] 마디별 (S번호, 자수)."""
    자리 = [(m.start(), m.group(1)) for m in RE_S.finditer(블록)]
    out = []
    for k, (st, s) in enumerate(자리):
        end = 자리[k + 1][0] if k + 1 < len(자리) else len(블록)
        out.append((s, end - st))
    return out


def B3본문(프롬프트: str) -> str:
    return assemble_context.블록분해(프롬프트).get("B3", "")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(_여기, "P3_분해_전.json"))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--top-k", type=int, default=5)
    a = ap.parse_args()

    포획: dict = {}
    원조립 = orchestrate.조립

    def spy(cur, 질문, 정규화, **kw):
        p, s맵, 사슬 = 원조립(cur, 질문, 정규화, **kw)
        포획.clear()
        포획.update(프롬프트=p, s맵=dict(s맵), 폐포=list(kw.get("폐포") or []),
                    검색=list(kw.get("검색") or []), l3=len(kw.get("l3") or []))
        return p, s맵, 사슬

    orchestrate.조립 = spy
    assemble_context._P3_spy = True   # 표식 (원본 동작에는 영향 없음)

    with db.connect(autocommit=True) as conn:
        cur = conn.cursor()
        코퍼스 = eval_store.코퍼스버전(cur)
        문항 = eval_store.평가대상(cur)
        if a.limit:
            문항 = 문항[:a.limit]
        print(f"코퍼스버전 {코퍼스} · 채점대상 {len(문항)}문항 · top_k={a.top_k}", flush=True)

        # 부속표 판별용 조문 메타 (한 번에 읽어 캐시)
        메타: dict[int, tuple] = {}

        def 조문메타(ids: list[int]) -> None:
            새 = [i for i in ids if i not in 메타]
            if not 새:
                return
            cur.execute("""SELECT article_id, doc_id, 조번호, 조제목, length(본문)
                             FROM corpus.doc_articles WHERE article_id = ANY(%s)""", (새,))
            for r in cur.fetchall():
                메타[r[0]] = (r[1], r[2], r[3], r[4])

        # 정답 근거 좌표 — B3 대표화(DEDUP)가 근거를 잘라내는지 보는 대조군
        정답좌표: dict[int, set] = {}
        for m in 문항:
            cur.execute("SELECT c.doc_id, c.조번호 FROM eval.golden_chunks gc "
                        "JOIN corpus.chunks c ON c.chunk_id = gc.chunk_id "
                        "WHERE gc.gold_id = %s", (m["gold_id"],))
            정답좌표[m["gold_id"]] = {(r[0], r[1]) for r in cur.fetchall()}

        결과 = []
        t0 = time.time()
        for i, m in enumerate(문항, 1):
            gid = m["gold_id"]
            사업 = eval_store.사업키(m["사업명"])
            포획.clear()
            try:
                r = orchestrate.판정(m["질문"], 사업명=사업, dry=True, top_k=a.top_k,
                                    conn=conn, 기록=False)
            except Exception as e:
                결과.append({"gold_id": gid, "오류": f"{type(e).__name__}: {e}",
                             "세트": m["세트"], "적용범위": m["적용범위"]})
                continue

            프롬프트 = 포획.get("프롬프트") or ""
            폐포 = 포획.get("폐포") or []
            조문메타(폐포)
            블록 = 블록분해(프롬프트)
            b3 = B3본문(프롬프트)
            b3조각 = S조각(b3)

            표 = [aid for aid in 폐포
                  if aid in 메타 and (RE_부속.match(메타[aid][1] or "")
                                     or RE_부속.match(메타[aid][2] or ""))]
            표자수 = sum(메타[aid][3] for aid in 표)
            폐포자수 = sum(메타[aid][3] for aid in 폐포 if aid in 메타)

            결과.append({
                "gold_id": gid, "세트": m["세트"], "적용범위": m["적용범위"],
                "사업명": m["사업명"], "정답": m["정답판정"],
                "프롬프트길이": r.get("프롬프트길이"),
                "프롬프트길이_실측": len(프롬프트),
                "s맵크기": r.get("s맵크기"),
                "블록": 블록,
                "B3_S조각수": len(b3조각),
                "B3_최대조각": max([c for _, c in b3조각], default=0),
                "폐포수": len(폐포), "폐포자수": 폐포자수,
                "표블록수": len(표), "표블록자수": 표자수,
                "표목록": [{"article_id": aid, "doc_id": 메타[aid][0],
                            "조번호": 메타[aid][1], "자수": 메타[aid][3]} for aid in 표],
                "top5": list((r.get("검색") or {}).get("top5") or []),
                "정답좌표수": len(정답좌표[gid]),
                "경로": r.get("경로"),
            })
            if i % 10 == 0 or i == len(문항):
                print(f"  {i}/{len(문항)} · {time.time()-t0:.0f}초", flush=True)

    with open(a.out, "w", encoding="utf-8") as f:
        json.dump({"코퍼스버전": 코퍼스, "top_k": a.top_k, "문항수": len(결과),
                   "항목": 결과}, f, ensure_ascii=False, indent=1)
    print(f"\n저장 {a.out}")

    # ── 요약 ────────────────────────────────────────────────────────────
    산 = [x for x in 결과 if "오류" not in x]

    def 표줄(이름: str, 부분: list[dict]) -> str:
        if not 부분:
            return f"{이름:<12} 0문항"
        n = len(부분)
        총 = [x["프롬프트길이_실측"] for x in 부분]
        b3 = [x["블록"].get("B3", 0) for x in 부분]
        표수 = [x["표블록수"] for x in 부분]
        표자 = [x["표블록자수"] for x in 부분]
        s = [x["s맵크기"] or 0 for x in 부분]
        return (f"{이름:<12} {n:>3}문항 | 총 평균{sum(총)//n:>7,} 최대{max(총):>7,} "
                f"| B3 평균{sum(b3)//n:>7,} 최대{max(b3):>7,} "
                f"| 표 평균{sum(표수)/n:>4.1f} 최대{max(표수):>2} "
                f"| 표자수 평균{sum(표자)//n:>7,} 최대{max(표자):>7,} "
                f"| S 평균{sum(s)//n:>4} 최대{max(s):>4}")

    print()
    print(표줄("전체", 산))
    # 🔴 `적용범위` 는 '공통(지침 제14차)' 아니면 NULL 이다(실측 26/67). 0_현황 의
    #    「공통 26 · 사업지정 67」 과 같은 갈래가 되게 NULL 을 사업지정으로 읽는다.
    공통 = [x for x in 산 if (x["적용범위"] or "").startswith("공통")]
    print(표줄("공통", 공통))
    print(표줄("사업지정", [x for x in 산 if not (x["적용범위"] or "").startswith("공통")]))
    for k in sorted({x["세트"] for x in 산}):
        print(표줄(f"세트:{k}", [x for x in 산 if x["세트"] == k]))
    오류 = [x for x in 결과 if "오류" in x]
    if 오류:
        print(f"\n🔴 오류 {len(오류)}건: {[x['gold_id'] for x in 오류][:10]}")


if __name__ == "__main__":
    main()
