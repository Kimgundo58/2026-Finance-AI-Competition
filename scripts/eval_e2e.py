# -*- coding: utf-8 -*-
"""D5 — 골든셋 실전 E2E. `orchestrate.판정()` 을 끝까지 돌리고 `eval.runs` 에 남긴다.

**오늘 이게 내는 유일하게 중요한 숫자.**

    판정층 격리 (근거를 정답으로 주면)   일치율 84.7% · 치명 오답 0
    검색이 근거를 찾는 비율               hit@5 52.9%
    ─────────────────────────────────────────────────────────────
    실전 E2E                              ← 여기. 둘 사이의 낙폭이 내일 무엇을 고칠지 정한다

**채점은 전부 결정론적이다.** 계약 §10 이 LLM-as-judge(RAGAS 등)를 금지한다 —
심판이 LLM 이면 같은 산출물에 다른 점수가 나온다.
  · 판정 일치   4-way (가능/조건부/불가/판단불가) 문자열 일치
  · 치명 오답   정답이 불가·조건부인데 예측이 '가능'  🔴 1건이라도 나오면 머지 금지
  · 인용 정확   예측 인용의 doc_id·조번호가 `eval.golden_chunks` 고정분과 겹치는가
  · 근거 적중   top5 ∩ 고정 정답청크 (검색이 근거를 물어왔는가)

**필수 분해 출력** (계약 §8-D5): 공통 / 사업지정 / L3경로.
🔴 D2 이후 공통 문항은 `사업명 IS NULL AND 적용범위 IS NOT NULL` 이다.
   `사업명='공통(지침 제14차)'` 로 가르던 옛 코드는 여기서 0건이 된다.

**판단불가율을 같이 낸다.** 계약 §7: E2E 판단불가율이 0% 여도 실패다 —
격리에서 0% 였던 건 근거를 정답으로 줬기 때문이고, hit@5 52.9% 인 실전에서 0% 면
근거 없이 답을 만들고 있다는 뜻이다.

실행:
    PYTHONIOENCODING=utf-8 python scripts/eval_e2e.py --dry        # LLM 없이 배관만 (GPU 전)
    PYTHONIOENCODING=utf-8 python scripts/eval_e2e.py              # 실전. GPU 창에서
    PYTHONIOENCODING=utf-8 python scripts/eval_e2e.py --limit 5 --dry
    PYTHONIOENCODING=utf-8 python scripts/eval_e2e.py --세트 본세트
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eval_store  # noqa: E402

DSN = eval_store.DSN

판정4 = ("가능", "조건부", "불가", "판단불가")


def _치명(정답: str, 예측: str) -> bool:
    """🔴 오답 비대칭. 안 되는 걸 된다고 하는 것만이 치명이다.
    반대(되는 걸 안 된다고)는 손해지 사고가 아니다."""
    return 정답 in ("불가", "조건부") and 예측 == "가능"


def _인용좌표(응답: dict) -> set[tuple]:
    """예측 인용에서 (doc_id, 조번호) 를 뽑는다. 키 이름이 갈릴 수 있어 넓게 받는다."""
    out = set()
    for c in 응답.get("인용목록") or []:
        if not isinstance(c, dict):
            continue
        doc = c.get("doc_id") or c.get("doc")
        조 = c.get("조번호") or c.get("조")
        if doc:
            out.add((doc, 조))
    return out


def 실행(*, dry: bool, limit: int | None, 세트: str | None, 라벨: str | None,
        top_k: int, 기록: bool) -> int:
    with psycopg.connect(DSN) as conn:
        cur = conn.cursor()

        문항 = eval_store.평가대상(cur, 세트=세트)
        if limit:
            문항 = 문항[:limit]
        if not 문항:
            sys.exit("평가 대상이 0건이다. scripts/pin_golden_chunks.py 를 먼저 돌려라.")

        # 고정 정답 좌표를 미리 읽는다 — 문항마다 재계산하지 않는다(결정성).
        정답청크: dict[int, set[int]] = {}
        정답좌표: dict[int, set[tuple]] = {}
        for m in 문항:
            gid = m["gold_id"]
            정답청크[gid] = eval_store.정답청크(cur, gid)
            # 🔴 정답 좌표는 `golden_chunks.조번호` 가 아니라 **그 청크의 좌표**로 잡는다.
            #    golden_chunks.조번호 는 골든셋 원표기('제20조①' · '[붙임2] 외주용역비
            #    유의사항')를 그대로 보존한 것이라 인용 쪽 표기('제20조' · '붙임2')와 다르다.
            #    실측: 104행 중 85행이 형식 불일치 → 그대로 비교하면 **인용적중이 허위 0%** 로
            #    나온다. chunk_id 는 이미 고정돼 있으니 corpus.chunks 에서 좌표를 읽으면
            #    표기가 자동으로 맞춰지고 정본이 한 곳(chunks)으로 유지된다.
            cur.execute("SELECT c.doc_id, c.조번호 FROM eval.golden_chunks gc "
                        "JOIN corpus.chunks c ON c.chunk_id = gc.chunk_id "
                        "WHERE gc.gold_id = %s", (gid,))
            정답좌표[gid] = {(r[0], r[1]) for r in cur.fetchall()}

        print(f"E2E {'드라이런' if dry else '실전'} · {len(문항)}문항 "
              f"(세트={세트 or '전체'}) · top_k={top_k}\n")

        import orchestrate  # 여기서 import 한다 — --help 만 볼 때 모델을 안 올리게

        items: list[dict] = []
        오류: list[tuple[int, str]] = []
        t0 = time.time()

        for i, m in enumerate(문항, 1):
            gid = m["gold_id"]
            사업 = eval_store.사업키(m["사업명"])
            try:
                r = orchestrate.판정(m["질문"], 사업명=사업, dry=dry, top_k=top_k,
                                    conn=conn, 기록=False)
            except Exception as e:
                오류.append((gid, f"{type(e).__name__}: {e}"))
                traceback.print_exc(limit=2)
                # 🔴 예외도 판단불가다. 조용히 빼면 분모가 흔들린다 (계약 §7).
                r = {"판정": "판단불가", "요약": f"[예외] {type(e).__name__}",
                     "강등코드": [], "강등사유": [f"eval_e2e 예외: {e}"],
                     "경로": "예외", "인용목록": []}

            예측 = r.get("판정") or "판단불가"
            정답 = m["정답판정"]
            검색 = r.get("검색") or {}
            top5 = list(검색.get("top5") or [])

            items.append({
                "gold_id": gid,
                "예측": 예측,
                "정답": 정답,
                "적중": 예측 == 정답,
                "원출력": {
                    "판정": 예측,
                    "요약": r.get("요약"),
                    "신뢰등급": r.get("신뢰등급"),
                    "인용목록": r.get("인용목록") or [],
                    "전제목록": r.get("전제목록") or [],
                    "강등코드": r.get("강등코드") or [],
                    "강등사유": r.get("강등사유") or [],
                    "경로": r.get("경로"),
                    "실패단계": r.get("실패단계"),
                    "게이트값": 검색.get("게이트값"),
                    "지연ms": r.get("지연ms") or {},
                    "s맵": r.get("s맵") or {},
                    # 채점 재료. 나중에 재채점할 때 다시 안 돌려도 되게 같이 남긴다
                    "top5": top5,
                    "근거적중": bool(set(top5) & 정답청크[gid]),
                    "인용적중": bool(_인용좌표(r) & 정답좌표[gid]),
                    "치명": _치명(정답, 예측),
                    "세트": m["세트"],
                    "적용범위": m["적용범위"],
                    "사업명": m["사업명"],
                },
            })
            if i % 10 == 0 or i == len(문항):
                print(f"  {i}/{len(문항)} · {time.time()-t0:.0f}초", flush=True)

        경과 = time.time() - t0

    # ── 집계 ────────────────────────────────────────────────────────────
    def 묶음(pred) -> list[dict]:
        return [it for it in items if pred(it)]

    def 지표(부분: list[dict]) -> dict:
        n = len(부분) or 1
        return {
            "문항수": len(부분),
            "일치율": round(sum(it["적중"] for it in 부분) / n * 100, 1),
            "치명오답": sum(it["원출력"]["치명"] for it in 부분),
            "판단불가율": round(sum(it["예측"] == "판단불가" for it in 부분) / n * 100, 1),
            "근거적중률": round(sum(it["원출력"]["근거적중"] for it in 부분) / n * 100, 1),
            "인용적중률": round(sum(it["원출력"]["인용적중"] for it in 부분) / n * 100, 1),
        }

    전체 = 지표(items)
    분해 = {
        "공통": 지표(묶음(lambda it: it["원출력"]["적용범위"] is not None)),
        "사업지정": 지표(묶음(lambda it: it["원출력"]["사업명"] is not None)),
        "L3경로": 지표(묶음(lambda it: "L3" in (it["원출력"]["경로"] or ""))),
    }
    for s in sorted({it["원출력"]["세트"] for it in items}):
        분해[f"세트:{s}"] = 지표(묶음(lambda it, s=s: it["원출력"]["세트"] == s))

    # 4-way 혼동행렬 — "어디로 틀렸나" 는 일치율 한 숫자로는 안 보인다
    혼동 = {f"{a}->{b}": 0 for a in 판정4 for b in 판정4}
    for it in items:
        키 = f"{it['정답']}->{it['예측']}"
        if 키 in 혼동:
            혼동[키] += 1
    혼동 = {k: v for k, v in 혼동.items() if v}

    코드빈도: dict[str, int] = {}
    for it in items:
        for c in it["원출력"]["강등코드"]:
            코드빈도[c] = 코드빈도.get(c, 0) + 1

    # 🔴 경로 빈도 — 일치율 한 숫자로는 "어느 분기에서 죽었나" 가 안 보인다.
    #    드라이런에서 이게 전부 'dry중단' 이 아니면 배관이 중간에 끊긴 것이다.
    경로빈도: dict[str, int] = {}
    for it in items:
        k = it["원출력"]["경로"] or "(없음)"
        경로빈도[k] = 경로빈도.get(k, 0) + 1

    # top5 를 한 건도 못 받았으면 근거적중률은 0% 가 아니라 **미측정**이다.
    # 0% 로 적으면 "검색이 다 틀렸다" 로 오독된다.
    근거측정 = any(it["원출력"]["top5"] for it in items)

    if not 근거측정:
        for g in [전체, *분해.values()]:
            g["근거적중률"] = None
    지표전체 = {**전체, "분해": 분해, "혼동": 혼동, "강등코드빈도": 코드빈도,
              "경로빈도": 경로빈도, "근거측정": 근거측정,
              "소요초": round(경과, 1), "오류": len(오류)}

    # ── 출력 ────────────────────────────────────────────────────────────
    print(f"\n{'='*66}\nE2E {'드라이런' if dry else '실전'} 결과  ({경과:.0f}초)\n{'='*66}")
    머리 = f"{'구간':12}{'문항':>5}{'일치율':>9}{'치명':>6}{'판단불가':>10}{'근거적중':>10}{'인용적중':>10}"
    print(머리)
    print("-" * len(머리))

    def 줄(이름, g):
        근거 = f"{g['근거적중률']:9.1f}%" if g["근거적중률"] is not None else f"{'미측정':>9} "
        print(f"{이름:12}{g['문항수']:5}{g['일치율']:8.1f}%{g['치명오답']:6}"
              f"{g['판단불가율']:9.1f}%{근거}{g['인용적중률']:9.1f}%")

    줄("전체", 전체)
    for k, v in 분해.items():
        if v["문항수"]:
            줄("  " + k, v)

    print("\n4-way 혼동 (정답->예측, 0건 생략)")
    for k, v in sorted(혼동.items(), key=lambda x: -x[1]):
        표 = "  🔴" if _치명(k.split("->")[0], k.split("->")[1]) else "    "
        print(f"{표} {k:22} {v}")

    print("\n경로 빈도 (어느 분기에서 끝났나)")
    for k, v in sorted(경로빈도.items(), key=lambda x: -x[1]):
        print(f"    {k:46} {v}")
    if not 근거측정:
        print("    ⚠️ top5 를 한 건도 못 받았다 — 근거적중률은 0% 가 아니라 미측정이다.")

    if 코드빈도:
        print("\n강등코드 빈도")
        for k, v in sorted(코드빈도.items(), key=lambda x: -x[1]):
            print(f"    {k:26} {v}")

    if 오류:
        print(f"\n🔴 예외 {len(오류)}건 (판단불가로 계상, 분모에서 빼지 않았다)")
        for gid, msg in 오류[:10]:
            print(f"    gold_id={gid} {msg}")

    치명 = 전체["치명오답"]
    if 치명:
        print(f"\n🔴 치명 오답 {치명}건 — 계약 §7 정지 조건. 머지 금지.")
        for it in items:
            if it["원출력"]["치명"]:
                print(f"    gold_id={it['gold_id']} 정답={it['정답']} 예측={it['예측']}")
    if not dry and 전체["판단불가율"] == 0.0:
        print("\n🔴 판단불가율 0% — 계약 §7 이 이것도 실패로 본다. "
              "hit@5 52.9% 인 실전에서 0% 면 근거 없이 답을 만들고 있다는 뜻이다.")

    # ── 적재 ────────────────────────────────────────────────────────────
    run_id = None
    if 기록:
        run_id = eval_store.기록(
            {"종류": "e2e",
             "설정": {"dry": dry, "top_k": top_k, "세트": 세트, "limit": limit,
                    "정답고정": "eval.golden_chunks(D3)",
                    "채점": "결정론 4-way + 치명오답 + 근거/인용 적중"},
             "문항수": len(items),
             "지표": 지표전체,
             "라벨": 라벨 or ("E2E 드라이런" if dry else "E2E 실전"),
             "비고": None},
            items)
        print(f"\neval.runs 기록 완료 — run_id = {run_id}")
    else:
        print("\n(--no-log) eval.runs 에 남기지 않았다")

    if 치명:
        sys.exit(2)          # 🔴 비0 종료로 머지를 막는다
    return run_id or 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="LLM 없이 배관만. GPU 열기 전 필수")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--세트", choices=["본세트", "적대적"])
    ap.add_argument("--top-k", type=int, default=5, dest="top_k")
    ap.add_argument("--라벨")
    ap.add_argument("--no-log", action="store_true", help="eval.runs 에 남기지 않는다")
    a = ap.parse_args()
    실행(dry=a.dry, limit=a.limit, 세트=a.세트, 라벨=a.라벨,
       top_k=a.top_k, 기록=not a.no_log)


if __name__ == "__main__":
    main()
