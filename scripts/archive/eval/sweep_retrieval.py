# -*- coding: utf-8 -*-
"""D7 — 검색 하이퍼파라미터 자동 스윕. 전건을 `eval.runs` 종류='retrieval' 로 남긴다.

**GPU 가 전혀 들지 않는다.** 질문 임베딩은 CPU KURE-v1 로 **74개를 한 번만** 계산해
메모리에 들고, 나머지는 SQL + 파이썬 산술이다. 밤새 돌려도 공짜다.

**축 4개** (계약 §8-D7 + 실측상 빠지면 해석이 안 되는 것 하나)

    W_DENSE   0.5 ~ 1.0        RRF 가중. W_SPARSE = 1 - W_DENSE
    RRF_K     5·10·20·60·120   RRF 감쇠상수 1/(K+순위). 현재 기본 60
    후보K     20·50·100        dense·BM25 각각의 후보 깊이. 현재 기본 50
                               🔴 이걸 빼면 RRF_K 효과와 구분이 안 된다 — 얕은 후보에서는
                                  감쇠상수를 아무리 바꿔도 순위가 안 흔들린다
    사업필터  끔 / 켬          C7 실측이 이미 켜는 쪽 우위를 보였다. 축에 넣어 재확인한다

    게이트임계 τ               별도 축이 아니라 **각 조합의 부가 지표**로 낸다.
                               τ 는 순위를 바꾸지 않고 "판단불가로 보낼지"만 가른다.

**정답은 `eval.golden_chunks` 고정분**(D3)이다. 매 실행 원문 부분일치로 되짚지 않는다 —
그래야 조합 간 비교가 성립한다.

🔴 **유의미 판정 규칙 (계약 §7).** 정답셋 74문항에서 1문항 = 1.4%p 다.
   **3문항(4.1%p) 미만의 차이는 노이즈다.** "최적" 이라고 부르지 않는다.
   이 스크립트는 기준값 대비 +3문항 이상인 조합만 `유의미` 로 표시한다.

🔴 **retrieve.py 는 C 소유다.** 여기서 기본값을 고치지 않는다. 최적값이 나와도
   A 에게 `BLOCKED` 로 제안만 한다.

실행:
    PYTHONIOENCODING=utf-8 python scripts/archive/eval/sweep_retrieval.py            # 전체 그리드
    PYTHONIOENCODING=utf-8 python scripts/archive/eval/sweep_retrieval.py --빠름     # 축소 그리드
    PYTHONIOENCODING=utf-8 python scripts/archive/eval/sweep_retrieval.py --no-log
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


import argparse
import itertools
import os
import sys
import time

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import db  # noqa: E402
import eval_store  # noqa: E402
import retrieve  # noqa: E402

DSN = db.DSN

W_그리드 = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
RRFK_그리드 = [5, 10, 20, 60, 120]
후보K_그리드 = [20, 50, 100]
필터_그리드 = [False, True]
게이트_그리드 = [0.00, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60]

KS = (1, 5, 10, 20)
유의미_문항 = 3          # 🔴 이 미만은 노이즈다 (계약 §7)


def 지표(순위별: list[tuple[list[int], set[int]]]) -> dict:
    """hit@k · MRR. 정답이 상위 k 안에 하나라도 있으면 적중이다."""
    n = len(순위별) or 1
    hit = {k: 0 for k in KS}
    rr = 0.0
    for 순위, 정답 in 순위별:
        첫 = next((i for i, c in enumerate(순위, 1) if c in 정답), None)
        if 첫:
            rr += 1.0 / 첫
            for k in KS:
                if 첫 <= k:
                    hit[k] += 1
    out = {f"hit@{k}": round(hit[k] / n * 100, 1) for k in KS}
    out["hit@5_문항"] = hit[5]
    out["MRR"] = round(rr / n, 4)
    return out


def 게이트분해(순위별, 게이트값들: list[float]) -> dict:
    """τ 별로 (통과율, 통과분 hit@5, 차단분 hit@5).

    🔴 읽는 법: 차단분 hit@5 가 높으면 그 τ 는 **맞는 근거를 버리고 있다**.
       통과분 hit@5 만 보고 "임계를 올릴수록 좋아진다" 고 읽으면 안 된다 —
       분모가 줄어서 오르는 것뿐이다.
    """
    out = {}
    n = len(순위별) or 1
    for τ in 게이트_그리드:
        통과 = [(r, a) for (r, a), g in zip(순위별, 게이트값들) if g >= τ]
        차단 = [(r, a) for (r, a), g in zip(순위별, 게이트값들) if g < τ]
        out[f"τ={τ:.2f}"] = {
            "통과": len(통과),
            "통과율": round(len(통과) / n * 100, 1),
            "통과_hit@5": 지표(통과)["hit@5"] if 통과 else None,
            "차단_hit@5": 지표(차단)["hit@5"] if 차단 else None,
            "차단_적중문항": 지표(차단)["hit@5_문항"] if 차단 else 0,
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--빠름", action="store_true", help="W 3점 · RRF_K 3점 · 후보K 2점")
    ap.add_argument("--no-log", action="store_true")
    a = ap.parse_args()

    W들 = [0.5, 0.75, 1.0] if a.빠름 else W_그리드
    RRFK들 = [10, 60, 120] if a.빠름 else RRFK_그리드
    후보K들 = [50, 100] if a.빠름 else 후보K_그리드

    with psycopg.connect(DSN) as conn:
        cur = conn.cursor()
        문항 = eval_store.평가대상(cur)
        if not 문항:
            sys.exit("평가 대상 0건. scripts/archive/eval/pin_golden_chunks.py 를 먼저 돌려라.")
        정답 = {m["gold_id"]: eval_store.정답청크(cur, m["gold_id"]) for m in 문항}
        코퍼스 = eval_store.코퍼스버전(cur)

        print(f"골든셋 {len(문항)}문항 · 코퍼스 {코퍼스}")
        print(f"그리드 W{len(W들)} × RRF_K{len(RRFK들)} × 후보K{len(후보K들)} × 필터2 "
              f"= {len(W들)*len(RRFK들)*len(후보K들)*2} 조합 "
              f"(τ {len(게이트_그리드)}점은 각 조합의 부가 지표)")
        print(f"🔴 1문항 = {100/len(문항):.1f}%p · "
              f"{유의미_문항}문항({유의미_문항*100/len(문항):.1f}%p) 미만 차이는 노이즈\n")

        # ── ① 임베딩 1회 (CPU). 이 뒤로는 GPU 도 모델도 안 쓴다 ──────────────
        t = time.time()
        retrieve.워밍업()
        벡터 = {}
        for i, m in enumerate(문항, 1):
            벡터[m["gold_id"]] = retrieve.임베딩(m["질문"])
            if i % 20 == 0:
                print(f"  임베딩 {i}/{len(문항)}", flush=True)
        print(f"임베딩 {len(문항)}건 {time.time()-t:.0f}초 (CPU · 이후 재사용)\n")

        # ── ② 후보 랭킹 캐시. (후보K, 필터) 조합마다 한 번만 SQL 을 친다 ──────
        t = time.time()
        캐시: dict[tuple, dict] = {}
        for 후보K, 필터 in itertools.product(후보K들, 필터_그리드):
            d_r, b_r, 게이트 = {}, {}, {}
            for m in 문항:
                gid, 사업 = m["gold_id"], eval_store.사업키(m["사업명"])
                dn = retrieve.dense(cur, 벡터[gid], k=후보K, 사업명=사업, 사업필터=필터)
                d_r[gid] = [c for c, _ in dn]
                게이트[gid] = dn[0][1] if dn else 0.0
                b_r[gid] = retrieve.sparse(cur, m["질문"], k=후보K, 사업명=사업, 사업필터=필터)
            캐시[(후보K, 필터)] = {"dense": d_r, "bm25": b_r, "게이트": 게이트}
            print(f"  후보 캐시 후보K={후보K} 필터={필터} · {time.time()-t:.0f}초", flush=True)
        print()

        # ── ③ 그리드. 여기부터는 순수 산술이라 한 조합이 밀리초다 ────────────
        결과 = []
        for 후보K, 필터, rrfk, w in itertools.product(후보K들, 필터_그리드, RRFK들, W들):
            c = 캐시[(후보K, 필터)]
            순위별, 게이트값들 = [], []
            for m in 문항:
                gid = m["gold_id"]
                순위 = retrieve.rrf([c["dense"][gid], c["bm25"][gid]],
                                   k=rrfk, 가중=(w, round(1.0 - w, 4)))
                순위별.append((순위, 정답[gid]))
                게이트값들.append(c["게이트"][gid])
            g = 지표(순위별)
            결과.append({
                "설정": {"W_DENSE": w, "W_SPARSE": round(1.0 - w, 4), "RRF_K": rrfk,
                       "후보K": 후보K, "사업필터": 필터, "문항수": len(문항),
                       "정답고정": "eval.golden_chunks(D3)"},
                "지표": {**g, "게이트": 게이트분해(순위별, 게이트값들)},
            })

        # dense·BM25 단독도 같은 문항으로 재본다 — RRF 가 실제로 이기는지의 대조군
        단독 = {}
        for 후보K, 필터 in itertools.product(후보K들, 필터_그리드):
            c = 캐시[(후보K, 필터)]
            for 이름, 키 in (("dense", "dense"), ("BM25", "bm25")):
                단독[(이름, 후보K, 필터)] = 지표(
                    [(c[키][m["gold_id"]], 정답[m["gold_id"]]) for m in 문항])

    # ── ④ 판독 ──────────────────────────────────────────────────────────────
    # 🔴 기준은 `retrieve.사업필터_기본` 이 아니라 **실제로 운영되는 설정**이다.
    #    A 의 orchestrate 는 `retrieve.사업필터_기본` 을 False 로 둔 채 호출부에서 켠다
    #    (`orchestrate.사업필터` — 환경변수 SUDDOE_사업필터). 모듈 기본값을 기준으로 잡으면
    #    "사업필터를 켠 효과" 가 하이퍼파라미터 개선으로 둔갑한다. 실제로 그렇게 읽힐 뻔했다:
    #    필터 OFF 기준으로는 최고 조합이 +5문항이지만, 실운영(필터 ON) 기준으로는 +1문항이다.
    try:
        import orchestrate as _orch
        운영_필터 = bool(getattr(_orch, "사업필터", retrieve.사업필터_기본))
    except Exception:
        운영_필터 = retrieve.사업필터_기본
    현재 = next((r for r in 결과
                if r["설정"]["W_DENSE"] == retrieve.W_DENSE
                and r["설정"]["RRF_K"] == retrieve.RRF_K
                and r["설정"]["후보K"] == retrieve.후보K
                and r["설정"]["사업필터"] == 운영_필터), None)
    기준문항 = 현재["지표"]["hit@5_문항"] if 현재 else max(
        r["지표"]["hit@5_문항"] for r in 결과)
    기준이름 = (f"실운영 설정(사업필터={운영_필터})" if 현재 else "그리드 최고(실운영 설정이 그리드 밖)")

    결과.sort(key=lambda r: (-r["지표"]["hit@5_문항"], -r["지표"]["MRR"]))

    print("=" * 92)
    print(f"상위 15 조합  (기준 = {기준이름} · hit@5 {기준문항}문항)")
    print("=" * 92)
    머리 = (f"{'W_D':>5}{'RRF_K':>7}{'후보K':>7}{'필터':>6}"
           f"{'hit@1':>8}{'hit@5':>8}{'hit@10':>8}{'hit@20':>8}{'MRR':>8}{'Δ문항':>7}  판정")
    print(머리)
    print("-" * len(머리))
    for r in 결과[:15]:
        s, g = r["설정"], r["지표"]
        Δ = g["hit@5_문항"] - 기준문항
        판정 = "유의미" if Δ >= 유의미_문항 else ("노이즈" if Δ > 0 else "")
        print(f"{s['W_DENSE']:5.2f}{s['RRF_K']:7}{s['후보K']:7}{str(s['사업필터']):>6}"
              f"{g['hit@1']:7.1f}%{g['hit@5']:7.1f}%{g['hit@10']:7.1f}%{g['hit@20']:7.1f}%"
              f"{g['MRR']:8.3f}{Δ:+7}  {판정}")

    if 현재:
        s, g = 현재["설정"], 현재["지표"]
        print(f"\n현재 기본값  W_DENSE={s['W_DENSE']} RRF_K={s['RRF_K']} "
              f"후보K={s['후보K']} 사업필터={s['사업필터']}  →  "
              f"hit@5 {g['hit@5']}% ({g['hit@5_문항']}문항) · MRR {g['MRR']}")

    print("\n단독 검색기 대조 (RRF 가 실제로 이기는가)")
    print(f"{'검색기':8}{'후보K':>7}{'필터':>6}{'hit@1':>8}{'hit@5':>8}{'hit@10':>8}{'MRR':>8}")
    for (이름, 후보K, 필터), g in sorted(단독.items()):
        print(f"{이름:8}{후보K:7}{str(필터):>6}{g['hit@1']:7.1f}%{g['hit@5']:7.1f}%"
              f"{g['hit@10']:7.1f}%{g['MRR']:8.3f}")

    최고 = 결과[0]
    Δ최고 = 최고["지표"]["hit@5_문항"] - 기준문항
    print("\n" + "=" * 92)
    if Δ최고 >= 유의미_문항:
        s = 최고["설정"]
        print(f"🔴 유의미 개선 후보: W_DENSE={s['W_DENSE']} RRF_K={s['RRF_K']} "
              f"후보K={s['후보K']} 사업필터={s['사업필터']}  (+{Δ최고}문항 = "
              f"{Δ최고*100/len(문항):.1f}%p)")
        print("   → retrieve.py 는 C 소유다. 여기서 고치지 않는다. A 에게 BLOCKED 로 제안만 한다.")
    else:
        print(f"그리드 최고가 기준 대비 +{Δ최고}문항뿐이다 "
              f"({Δ최고*100/len(문항):.1f}%p < {유의미_문항*100/len(문항):.1f}%p).")
        print("   → 유의미하지 않다. '최적' 이라고 부르지 않는다. 기본값을 바꿀 근거가 없다.")

    # 게이트 τ 판독은 최고 조합 기준으로 한 번만 낸다 (전 조합은 eval.runs 에 있다)
    print("\n게이트 임계 τ — 최고 조합 기준")
    print(f"{'τ':>7}{'통과':>7}{'통과율':>9}{'통과 hit@5':>12}{'차단 hit@5':>12}{'차단 적중':>10}")
    for τ, v in 최고["지표"]["게이트"].items():
        통 = f"{v['통과_hit@5']:.1f}%" if v["통과_hit@5"] is not None else "-"
        차 = f"{v['차단_hit@5']:.1f}%" if v["차단_hit@5"] is not None else "-"
        print(f"{τ:>7}{v['통과']:7}{v['통과율']:8.1f}%{통:>12}{차:>12}{v['차단_적중문항']:10}")
    print("   🔴 '차단 적중' = 그 τ 가 버린 문항 중 실제로 정답을 물어온 건수. "
          "이게 0 이 아니면 τ 를 올린 만큼 맞는 답을 판단불가로 보내고 있다.")

    # ── ⑤ 적재 — 전건 ───────────────────────────────────────────────────────
    if a.no_log:
        print("\n(--no-log) eval.runs 에 남기지 않았다")
        return
    t = time.time()
    ids = []
    for r in 결과:
        Δ = r["지표"]["hit@5_문항"] - 기준문항
        ids.append(eval_store.기록({
            "종류": "retrieval",
            "코퍼스버전": 코퍼스,
            "설정": r["설정"],
            "문항수": len(문항),
            "지표": {**r["지표"], "Δ문항_대비기준": Δ,
                   "유의미": Δ >= 유의미_문항, "유의미_기준문항": 유의미_문항},
            "라벨": (f"sweep W{r['설정']['W_DENSE']} K{r['설정']['RRF_K']} "
                   f"c{r['설정']['후보K']} f{int(r['설정']['사업필터'])}"),
            "비고": f"D7 스윕 · 기준={기준이름}({기준문항}문항)",
        }, []))
    print(f"\neval.runs 에 {len(ids)}건 기록 ({time.time()-t:.0f}초) · "
          f"run_id {min(ids)}~{max(ids)}")


if __name__ == "__main__":
    main()
