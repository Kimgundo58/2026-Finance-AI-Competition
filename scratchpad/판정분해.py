# -*- coding: utf-8 -*-
"""판정 run 전수 분해 — «왜 틀렸나» 를 원인 축으로 가른다.

🔴 이 스크립트의 존재 이유는 하나다: **오답을 한 덩어리로 세면 처방이 안 나온다.**
   run 194 에서 「일치율 44.7%」가 실은 「32.5%가 판정을 아예 못 받음」이었던 것처럼,
   분해하지 않으면 전송 결함이 판정 실력으로 읽힌다.

원인 축은 넷이고 «배타적» 이다 (위에서부터 먼저 걸리는 것으로 귀속):

    ① 실패경로     판정을 «못 받았다». 파이프라인이 넘어짐 (타임아웃·524·예외)
    ② 조기종료     게이트 A/B/C 에서 끊겼다. 판정 LLM 자체를 안 탔다
    ③ 근거 미도달  판정은 했는데 정답 근거가 프롬프트에 «없었다» → 검색 문제
    ④ 판정 오류    근거를 받고도 결론이 틀렸다 → 모델·프롬프트 문제

  ⇒ 「모델을 바꿔서 얻을 수 있는 상한」 = (정답 + ④) / 전체. ①②③ 은 모델 밖이다.

⚠️ 「근거적중」은 `eval_e2e` 가 `eval.golden_chunks(D3)` 기준으로 미리 채점해 둔 값을
   그대로 읽는다. 이 스크립트가 다시 계산하지 않는다 — 두 곳에서 계산하면 갈린다.

실행: PYTHONIOENCODING=utf-8 python scratchpad/판정분해.py <run_id>
"""
from __future__ import annotations

import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
from _lib import db  # noqa: E402

_4WAY = ("가능", "조건부", "불가", "판단불가")


def 적재(run_id: int):
    with db.connect() as c:
        머리 = c.execute("SELECT 라벨, 설정, 시작, 종료 FROM eval.runs WHERE run_id=%s",
                       (run_id,)).fetchone()
        rows = c.execute("""
            SELECT i.gold_id, i."예측", i."정답", i."적중", i."원출력",
                   g."사업명", g."세트", g."대상", g."평가범위", g."비목"
            FROM eval.run_items i JOIN eval.golden_set g USING (gold_id)
            WHERE i.run_id=%s ORDER BY i.gold_id""", (run_id,)).fetchall()
        gc = defaultdict(list)
        for gid, cid, doc in c.execute("""
                SELECT gold_id, chunk_id, doc_id FROM eval.golden_chunks
                WHERE gold_id IN (SELECT gold_id FROM eval.run_items WHERE run_id=%s)""",
                                       (run_id,)).fetchall():
            gc[gid].append((cid, doc))
        길이 = dict(c.execute("SELECT chunk_id, length(text) FROM corpus.chunks").fetchall())
    return 머리, rows, gc, 길이


def 원인(o: dict, 적중: bool) -> str:
    """오답 하나의 «귀속 원인». 위에서부터 먼저 걸리는 것 하나로만 센다."""
    if 적중:
        return "정답"
    if o.get("실패단계"):
        return "① 실패경로"
    if "4조립" not in (o.get("경로") or ""):
        return "② 조기종료"
    return "③ 근거미도달" if not o.get("근거적중") else "④ 판정오류"


def _칸(제목: str, 카운터: Counter, 전체: int | None = None) -> None:
    print(f"\n{제목}")
    for k, v in sorted(카운터.items(), key=lambda x: -x[1]):
        비 = f"  {v / 전체 * 100:5.1f}%" if 전체 else ""
        print(f"    {str(k):<32} {v:>4}{비}")


def _분위(v: list[int]) -> str:
    v = sorted(x for x in v if x is not None)
    if not v:
        return "없음"
    return (f"n={len(v):>4} p50={v[len(v) // 2]:>7} "
            f"p90={v[int(len(v) * .9)]:>7} max={v[-1]:>7}")


def main() -> int:
    run_id = int(sys.argv[1])
    머리, rows, gc, 길이 = 적재(run_id)
    if not rows:
        print(f"run {run_id} 문항 0건 — 아직 안 끝났거나 없는 run 이다")
        return 1
    라벨, cfg, 시작, 종료 = 머리
    cfg = cfg or {}
    N = len(rows)
    O = [r[4] or {} for r in rows]

    print("=" * 78)
    print(f"run {run_id}   {라벨}")
    print("=" * 78)
    print(f"문항 {N} · 시작 {시작} · 종료 {종료}")
    for k in ("변형", "동시", "폐포사용", "top_k", "b0_sha1", "코퍼스버전", "max_model_len"):
        if k in cfg:
            print(f"  {k:<12} {cfg[k]}")
    if cfg.get("git"):
        print(f"  {'git':<12} {str(cfg['git'].get('commit'))[:12]} · dirty={cfg['git'].get('dirty')}")
    if cfg.get("SUDDOE_플래그"):
        print(f"  {'SUDDOE':<12} {cfg['SUDDOE_플래그']}")
    if cfg.get("VLLM_URL"):
        print(f"  {'VLLM_URL':<12} {cfg['VLLM_URL']}")

    적중수 = sum(1 for r in rows if r[3])
    다수 = Counter(r[2] for r in rows).most_common(1)[0]
    치명 = [r for r in rows if (r[4] or {}).get("치명")]
    근거 = sum(1 for o in O if o.get("근거적중"))
    인용 = sum(1 for o in O if o.get("인용적중"))
    print(f"\n일치율 {적중수}/{N} = {적중수 / N * 100:.1f}%    "
          f"다수결기준선 {다수[1] / N * 100:.1f}% («{다수[0]}» 상수 모델)    "
          f"초과 {(적중수 - 다수[1]) / N * 100:+.1f}")
    print(f"치명 {len(치명)}건    근거적중 {근거 / N * 100:.1f}%    인용적중 {인용 / N * 100:.1f}%")

    원인표 = Counter(원인(o, r[3]) for r, o in zip(rows, O))
    _칸("원인 분해 (배타적 귀속 — 한 문항은 한 칸에만 든다)", 원인표, N)
    모델안 = 원인표.get("정답", 0) + 원인표.get("④ 판정오류", 0)
    print(f"\n  ⇒ 🔴 모델을 무엇으로 바꿔도 상한 {모델안 / N * 100:.1f}% (= 정답 + ④판정오류).")
    print(f"     나머지 {N - 모델안}건은 «모델 밖» 이다 — 파인튜닝·모델교체로 안 움직인다.")

    # 🔴 **실패했는데 «정답» 으로 채점된 문항.** 정답이 「판단불가」인 문항에서 파이프라인이
    #    넘어지면 결과도 「판단불가」라 «맞은 것으로 센다». 판정력의 증거가 아니라
    #    두 개의 «모름» 이 우연히 겹친 것이다. 일치율을 그만큼 부풀린다.
    공짜 = [r for r, o in zip(rows, O) if r[3] and o.get("실패단계")]
    if 공짜:
        print(f"\n  🔴 «실패했는데 정답» {len(공짜)}건 — 정답이 「판단불가」인 문항에서 파이프라인이")
        print(f"     넘어져 결과도 「판단불가」가 됐다. 판정력이 아니라 두 «모름» 이 겹친 것이다.")
        print(f"     이걸 빼면 일치율 {(적중수 - len(공짜)) / N * 100:.1f}% "
              f"(= {적중수 - len(공짜)}/{N}). gold_id {[r[0] for r in 공짜][:12]}")
        print(f"     ⚠️ 위 원인표의 「① 실패경로 {원인표.get('① 실패경로', 0)}」와 아래 ①절의 "
              f"실패 총계가 {len(공짜)}만큼 다른 이유가 이것이다")

    실패 = [(r, o) for r, o in zip(rows, O) if o.get("실패단계")]
    if 실패:
        print(f"\n① 실패경로 {len(실패)}건 — 판정을 «못 받았다». 오답이 아니라 «미측정» 이다")
        for st, n in Counter(o["실패단계"] for _, o in 실패).most_common():
            print(f"    {st:<12} {n}")
            사유 = Counter()
            for _, o in 실패:
                if o["실패단계"] != st:
                    continue
                s = " ".join(o.get("강등사유") or [])
                for 키 in ("HTTP 524", "TimeoutError", "RemoteDisconnected", "getaddrinfo",
                           "IncompleteRead", "ConnectionReset", "JSON 파싱", "스키마 밖",
                           "스트리밍 총 마감"):
                    if 키 in s:
                        사유[키] += 1
                        break
                else:
                    사유[s[:56] or "미상"] += 1
            for k, v in 사유.most_common():
                print(f"        {k:<30} {v}")
    else:
        print("\n① 실패경로 **0건** — 파이프라인이 한 문항도 안 넘어졌다")

    조기 = [(r, o) for r, o in zip(rows, O)
            if not o.get("실패단계") and "4조립" not in (o.get("경로") or "")]
    print(f"\n② 조기종료 {len(조기)}건 — 게이트에서 끊겨 판정 LLM 을 «안 탔다»")
    if 조기:
        _칸("   게이트별", Counter(o.get("게이트") or "?" for _, o in 조기))
        _칸("   경로별", Counter(o.get("경로") or "?" for _, o in 조기))
        맞 = sum(1 for r, _ in 조기 if r[3])
        print(f"    그중 정답 {맞}건 — 조기종료가 늘 오답인 건 «아니다» (게이트 A 즉답 불가 등)")

    print("\n판정 분포 — 정답 vs 예측 («말하지 못하는 판정» 이 보인다)")
    정c, 예c = Counter(r[2] for r in rows), Counter(r[1] for r in rows)
    for k in _4WAY:
        print(f"    {k:<8} 정답 {정c.get(k, 0):>4}   →   예측 {예c.get(k, 0):>4}")

    print("\n4-way 혼동 (정답→예측 · 상위 12)")
    for (a, b), n in Counter((r[2], r[1]) for r in rows if r[2] != r[1]).most_common(12):
        print(f"    {n:>4}  {a} → {b}{'   ← 치명 축' if b == '가능' and a in ('불가',) else ''}")

    불가 = [(r, o) for r, o in zip(rows, O) if r[1] == "판단불가"]
    선택 = [(r, o) for r, o in 불가 if not o.get("실패단계")]
    print(f"\n판단불가 {len(불가)}건 = 모델선택 {len(선택)} + 실패경로 {len(불가) - len(선택)}")
    print("    🔴 한 숫자로 세면 «잘림» 이 «판단» 으로 읽힌다 (CLAUDE.md 지표 절)")
    if 선택:
        근없 = sum(1 for _, o in 선택 if not o.get("근거적중"))
        print(f"    모델선택 {len(선택)}건 중 근거✗ {근없}건 "
              f"({근없 / len(선택) * 100:.0f}%) — 답할 재료 자체가 없었나")

    판정함 = [(r, o) for r, o in zip(rows, O)
              if not o.get("실패단계") and "4조립" in (o.get("경로") or "")]
    도달 = [1 for _, o in 판정함 if o.get("근거적중")]
    print(f"\n③ 검색 — 판정을 «탄» {len(판정함)}건 기준 근거적중 "
          f"{len(도달)}/{len(판정함)} = {len(도달) / max(len(판정함), 1) * 100:.1f}%")
    print("    🔴 전체 분모(위 근거적중)와 «다른 수» 다. 실패경로를 뺀 값이다")

    def 버킷(n: int) -> str:
        return ("~500" if n < 500 else "500~1k" if n < 1000
                else "1k~1.5k" if n < 1500 else "1.5k~")

    길이표: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r, o in 판정함:
        cs = [길이[c] for c, _ in gc.get(r[0], []) if c in 길이]
        if not cs:
            continue
        b = 버킷(max(cs))
        길이표[b][1] += 1
        길이표[b][0] += 1 if o.get("근거적중") else 0
    print("\n   정답청크 «최장» 길이별 근거적중")
    for b in ("~500", "500~1k", "1k~1.5k", "1.5k~"):
        h, t = 길이표.get(b, [0, 0])
        if t:
            print(f"      {b:<10} {h:>3}/{t:<4} {h / t * 100:5.1f}%"
                  f"{'   ← 절벽' if b == '1.5k~' and h / t < 0.35 else ''}")

    print("\n   골든셋 `대상` 별 근거적중 — 구조적 배제가 여기서 보인다")
    대상표: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r, o in 판정함:
        k = r[7] or "?"
        대상표[k][1] += 1
        대상표[k][0] += 1 if o.get("근거적중") else 0
    for k, (h, t) in sorted(대상표.items(), key=lambda x: -x[1][1]):
        비 = "   🔴 검색 필터가 원천 배제 (적용대상 IN 창업기업·공통)" if k == "주관기관" and h == 0 else ""
        print(f"      {k:<10} {h:>3}/{t:<4} {h / t * 100:5.1f}%{비}")

    print("\n   문서별 근거적중 (정답청크 1순위 문서 · 5건 이상 · 나쁜 순)")
    문서표: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r, o in 판정함:
        for _, doc in gc.get(r[0], [])[:1]:
            문서표[doc][1] += 1
            문서표[doc][0] += 1 if o.get("근거적중") else 0
    for k, (h, t) in sorted(문서표.items(), key=lambda x: x[1][0] / max(x[1][1], 1)):
        if t >= 5:
            print(f"      {str(k)[:46]:<48} {h:>3}/{t:<4} {h / t * 100:5.1f}%")

    print("\n강등코드 × 정오 — «오답을 골라내는가» (판정에 배선할지의 근거)")
    코드표: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r, o in zip(rows, O):
        for cd in o.get("강등코드") or []:
            코드표[cd][0 if r[3] else 1] += 1
    print(f"      {'코드':<26} {'정답':>5} {'오답':>5}   판정")
    for k, (ok, ng) in sorted(코드표.items(), key=lambda x: -sum(x[1])):
        판 = ("🔴 정답에 더 붙는다 = 순손실" if ok > ng
              else "선별력 없음" if ok == ng else "오답에 쏠림")
        print(f"      {k:<26} {ok:>5} {ng:>5}   {판}")
    if not 코드표:
        print("      (발화 0건)")

    print("\n계측 — 잘림(finish_reason)과 사고 토큰")
    print(f"    판정 종료이유 {dict(Counter((o.get('모델') or {}).get('종료이유') for o in O))}")
    ln = [r for r, o in zip(rows, O) if (o.get("모델") or {}).get("종료이유") == "length"]
    if ln:
        print(f"    🔴 판정이 «잘린» 문항 {len(ln)}건 — 그 판단불가는 모델 선택이 아니다. "
              f"gold_id {[r[0] for r in ln][:12]}")
    추 = [(o.get("모델") or {}).get("추론content길이") for o in O]
    if any(추):
        print(f"    판정 reasoning 길이   {_분위(추)}")
    else:
        print("    🔴 판정 reasoning 길이 «0건» — 계측이 비어 있다. "
              "「사고를 안 했다」로 읽지 마라 (이 run 에 계측이 없다는 뜻)")
    nm = [o.get("정규화메타") or {} for o in O]
    if any(x.get("추론content길이") for x in nm):
        print(f"    정규화 reasoning 길이 {_분위([x.get('추론content길이') for x in nm])}")
        print(f"    정규화 completion_tok {_분위([(x.get('토큰') or {}).get('completion_tokens') for x in nm])}")
        print(f"    정규화 종료이유 {dict(Counter(x.get('종료이유') for x in nm))}")
    else:
        print("    정규화 계측 없음 (이 run 에 `정규화메타` 가 안 실렸다)")

    print("\n지연 (ms)")
    for 단 in ("정규화", "검색", "조립", "판정LLM", "검증", "총"):
        v = [(o.get("지연ms") or {}).get(단) for o in O]
        if any(v):
            print(f"    {단:<8} {_분위(v)}")

    if 치명:
        print(f"\n🔴 치명 오답 {len(치명)}건 — 계약 §7 정지 조건")
        for r in 치명:
            print(f"    gold_id={r[0]} 사업={r[5]} 정답={r[2]} 예측={r[1]}")
            print(f"        {str((r[4] or {}).get('요약'))[:120]}")
    else:
        print("\n치명 오답 0건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
