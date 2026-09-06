# -*- coding: utf-8 -*-
"""D4 — 평가 결과 적재 (`eval.runs` · `eval.run_items`).

계약 §4 동결 인터페이스:

    eval_store.기록(run: dict, items: list[dict]) -> run_id
      run   {"종류":"e2e|retrieval|judge", "코퍼스버전":str, "설정":{...},
             "문항수":int, "지표":{...}}
      items [{"gold_id":int, "예측":str, "정답":str, "적중":bool, "원출력":{...}}]

**설계 이유 세 가지.**

1. `지표` 와 `원출력` 은 jsonb 통째로 받고 스키마 검증을 하지 않는다.
   종류마다 지표가 다르고(hit@5 vs 일치율 vs 판단불가율) A 가 넣는 원출력 키도
   늘어난다. 컬럼으로 박으면 새 지표마다 DDL 이 필요해지고 DDL 은 D 만 칠 수 있다 —
   밤새 돌리는 스윕이 스키마 변경을 기다리게 되는 건 말이 안 된다.

2. 🔴 `설정` 은 반드시 채운다. 재현의 유일한 근거다.
   내일 아침 "이 숫자가 무엇 때문에 나왔나"를 답할 수 있는 건 이 필드뿐이다.

3. `코퍼스버전` · `git커밋` 은 안 주면 여기서 실측해 채운다.
   사람이 안 적어서 비는 걸 막는다 — 규정 모음이 바뀌면 지표는 비교 불가가 된다.

**금지** — 계약 §10: LLM-as-judge 평가(RAGAS 등)를 여기에 붙이지 않는다.
심판이 LLM 이면 같은 산출물에 다른 점수가 나온다. 채점은 전부 결정론적이다.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import db                                                   # noqa: E402

DSN = db.DSN

_종류 = ("e2e", "retrieval", "judge")


def 코퍼스버전(cur) -> str:
    """판정 인덱스의 상태를 한 문자열로 압축한다.

    청크 수만으로는 부족하다 — 같은 수라도 내용이 바뀔 수 있다. 그래서
    (청크수, 임베딩수, refs수, doc수, 최대 chunk_id) 를 해시한다. 싸고 충분하다.
    """
    cur.execute("""
        SELECT (SELECT count(*) FROM corpus.chunks),
               (SELECT count(*) FROM corpus.chunks WHERE embedding IS NOT NULL),
               (SELECT count(*) FROM corpus.refs),
               (SELECT count(*) FROM corpus.documents),
               (SELECT coalesce(max(chunk_id),0) FROM corpus.chunks)""")
    n = cur.fetchone()
    h = hashlib.sha1("|".join(map(str, n)).encode()).hexdigest()[:8]
    return f"c{n[0]}-e{n[1]}-r{n[2]}-d{n[3]}-{h}"


def 골든고정(cur) -> str:
    """**정답지 고정**(`eval.golden_chunks`)의 상태를 한 문자열로 압축한다.

    🔴 왜 따로 두는가 (2026-09-06). `코퍼스버전()` 은 «검색 대상» 만 본다.
    그런데 hit@k 는 «검색 대상» 과 «정답 청크 집합» 둘 다에 달려 있고, 후자는
    `설정` 에 `"정답고정": "eval.golden_chunks(D3)"` 라는 **문자열 하나**로만 남아 있었다 —
    run 194 와 195 가 글자 하나 안 다르다. 즉 기록이 아니라 라벨이었다.
    실측: 0단계 표복구로 golden_chunks 가 404 -> 399 행이 됐는데 그 라벨은 안 변했다.

    🔴 `매칭방법 분포`를 반드시 넣는다. 같은 날 «행 수는 그대로인데 매칭방법만 바뀌는»
    일이 실제로 났다 — gold 380·389 가 표 복구로 근거원문이 청크에 축자로 잡히면서
    `조번호`(그 조의 청크 전부를 정답으로 치는 폴백) -> `원문일치`(정확히 1개)로 올라섰다.
    (총행, 실패행, gold_id 종수, max gc_id) 만으로는 이게 안 잡히는데, 정답 청크 집합이
    달라졌으니 hit@k 는 움직인다. 분포가 있어야 「정밀도가 올랐다」와 「행이 줄었다」가 갈린다.

    🔴 이 지문이 다른 두 run 은 hit@k 를 **빼면 안 된다** (`CLAUDE.md` 「조건이 다른 run
    끼리 hit@k·일치율을 빼지 않는다」). 지문이 «없는» run 은 이 함수가 생기기 전 run 이다 —
    옛 run 의 설정을 소급해 채우지 않는다. 그날의 기록이 아니게 된다.
    """
    cur.execute("""
        SELECT (SELECT count(*) FROM eval.golden_chunks),
               (SELECT count(*) FROM eval.golden_chunks WHERE 매칭방법='실패'),
               (SELECT count(DISTINCT gold_id) FROM eval.golden_chunks),
               (SELECT coalesce(max(gc_id),0) FROM eval.golden_chunks)""")
    n = cur.fetchone()
    cur.execute("SELECT 매칭방법, count(*) FROM eval.golden_chunks GROUP BY 1 ORDER BY 1")
    분포 = cur.fetchall()

    # 🔴 2026-09-06 — 씨앗에 `golden_set` 을 «더한다». 그전엔 golden_chunks «뿐» 이라
    #    «정답 라벨이 바뀌어도 지문이 안 움직였다». 실측으로 증명했다: 정답 3건을 실제로
    #    바꿔도 지문이 g399-f6-q315-144be751 로 «불변». hit@k 는 안 움직여도 «일치율은
    #    움직이는데», run 끼리 「같은 정답지였나」를 증명할 수단이 없었다.
    #    같은 날 오너 승인으로 정답판정 «5건»(545·546·566·615 -> 불가, 552 -> 조건부)과
    #    verified 190건 승격이 들어갔다 — 고치지 않았으면 그 run 이 «이전과 같은 정답지» 로
    #    보였을 것이다. 이 배치가 바로 이 결함이 물릴 자리였다.
    cur.execute("SELECT count(*), count(*) FILTER (WHERE verified) FROM eval.golden_set")
    총, 검증됨 = cur.fetchone()
    cur.execute("SELECT 정답판정, count(*) FROM eval.golden_set GROUP BY 1 ORDER BY 1")
    판정분포 = cur.fetchall()

    씨앗 = ("|".join(map(str, n)) + "|" + ",".join(f"{k}:{v}" for k, v in 분포)
            + f"|s{총}v{검증됨}|" + ",".join(f"{k}:{v}" for k, v in 판정분포))
    h = hashlib.sha1(씨앗.encode()).hexdigest()[:8]
    return f"g{n[0]}-f{n[1]}-q{n[2]}-s{총}v{검증됨}-{h}"


def _git커밋() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        더러움 = subprocess.run(["git", "status", "--porcelain"],
                                capture_output=True, text=True, timeout=10)
        if out.returncode:
            return None
        # 🔴 워킹트리가 더러우면 커밋 해시만으로는 재현이 안 된다. 그 사실을 남긴다.
        return out.stdout.strip() + ("+dirty" if 더러움.stdout.strip() else "")
    except Exception:
        return None


def 기록(run: dict, items: list[dict] | None = None, *, conn=None) -> int:
    """평가 1회를 남기고 run_id 를 돌려준다. items 는 없어도 된다(집계만 남기는 실행)."""
    종류 = run.get("종류")
    if 종류 not in _종류:
        raise ValueError(f"종류는 {_종류} 중 하나여야 한다: {종류!r}")

    with db.borrow(conn) as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO eval.runs
                 (종류, 코퍼스버전, git커밋, 설정, 문항수, 지표, 라벨, 비고, 종료)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())
               RETURNING run_id""",
            (종류,
             run.get("코퍼스버전") or 코퍼스버전(cur),
             run.get("git커밋") or _git커밋(),
             json.dumps(run.get("설정") or {}, ensure_ascii=False),
             run.get("문항수") if run.get("문항수") is not None
                 else (len(items) if items is not None else None),
             json.dumps(run.get("지표") or {}, ensure_ascii=False),
             run.get("라벨"),
             run.get("비고")),
        )
        run_id = cur.fetchone()[0]

        for it in items or []:
            cur.execute(
                """INSERT INTO eval.run_items (run_id, gold_id, 예측, 정답, 적중, 원출력)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (run_id, gold_id) DO UPDATE SET
                     예측=EXCLUDED.예측, 정답=EXCLUDED.정답,
                     적중=EXCLUDED.적중, 원출력=EXCLUDED.원출력""",
                (run_id, it.get("gold_id"), it.get("예측"), it.get("정답"),
                 it.get("적중"),
                 json.dumps(it.get("원출력") or {}, ensure_ascii=False, default=str)),
            )
        conn.commit()
        return run_id


def 정답청크(cur, gold_id: int) -> set[int]:
    """고정된 정답 청크 (D3 `eval.golden_chunks`). 원문 재매칭을 하지 않는다.

    🔴 여기가 평가의 결정성을 담보하는 지점이다. 매 실행 원문 부분일치로 되짚으면
       청킹이 바뀔 때마다 정답 집합이 조용히 달라진다.
    """
    cur.execute(
        "SELECT chunk_id FROM eval.golden_chunks WHERE gold_id=%s AND chunk_id IS NOT NULL",
        (gold_id,))
    return {r[0] for r in cur.fetchall()}


def 평가대상(cur, *, 세트: str | None = None, 범위밖포함: bool = False) -> list[dict]:
    """정답 청크가 고정된 문항 중 **평가 범위 안**의 것만. 분모를 여기서 한 번에 정한다.

    🔴 `평가범위='범위밖…'` 을 뺀다 (2026-09-01). `_골든셋_초안.json` 메타가
       «창업팀 전용 전환을 확정했다. 따라서 대상='주관기관' 16문항은 평가 범위 밖이다»
       라고 이미 결정해 놨는데, 그 축이 `load_db.py` INSERT 에서 빠져 DB 에 도달하지
       못했고 여기서도 거르지 못했다. 그동안 **창업팀 전용 서비스를 주관기관 문항
       21% 로 채점**하고 있었다.
       그 16문항은 근거가 주관기관 운영비 조인데 검색 필터가
       `적용대상 IN ('창업기업','공통')` 이라 **구조적으로 hit 이 불가능**하다 —
       리랭커·가중·임베딩 무엇으로도 안 움직인다. 분모에 두면 영원히 못 넘는 벽이
       «검색 성능» 으로 보고된다.
       `범위밖포함=True` 는 센터 화면이 되살아날 때를 위한 문이다. 기본은 제외.

    🔴 **`세트='L3'` 도 골든청크가 없어도 분모에 든다** (2026-09-07, ai-33 실측 정정).
       처음엔 "L3 문서가 코퍼스에 결손"이라고 진단했는데 틀렸다 — L3 는 `tenant.
       l3_articles` 에 정상 적재돼 있다(224조·파싱품질 warn·dangling 0). `eval.
       golden_chunks`·`corpus.chunks` 를 보는 이 필터가 애초에 L3 가 사는 스키마를
       안 본다 — 코퍼스 결손이 아니라 **이 하네스의 결손**이다. `pin_golden_chunks.py`
       로 못 고친다(그 스크립트도 corpus.chunks 만 본다). 그래서 L3 는 golden_chunks
       없이도 통과시키고, 인용정확도 채점은 `eval_e2e.py::지표()` 쪽에서 "정답청크가
       실제로 있는가"로 따로 뺀다(판정일치율은 그대로 채점된다 — L3 인용까지 채점하려면
       `tenant.l3_articles` 를 되짚는 채점기가 따로 필요한데, 그건 이번 범위 밖이다).

    🔴 **`정답판정='판단불가'` 는 골든청크가 없어도 분모에 든다** (2026-09-01).
       「골든청크가 고정된 것만 센다」는 조건이 **판단불가 문항을 구조적으로 배제**한다 —
       규범에 답이 없다는 것이 정답인 문항은 고정할 청크가 애초에 없기 때문이다.
       그 결과 오늘까지 채점 분모 65 의 정답 분포가 `불가 44 / 조건부 17 / 가능 4 /
       **판단불가 0**` 이었다. 시스템의 기본값이 판단불가인데 그게 옳은 상황을 한 번도
       묻지 않았고, 안전한 실패가 점수상 **처벌만** 받았다.
       근거 청크가 없으므로 이 문항들의 인용적중은 채점하지 않는다 (`eval_e2e` 가 뺀다).
    """
    cur.execute(
        """SELECT g.gold_id, g.세트, g.사업명, g.적용범위, g.질문, g.정답판정, g.비목,
                  g.대상, g.평가범위, g.채점모드
             FROM eval.golden_set g
            WHERE (g.정답판정 = '판단불가'
                   OR g.세트 = 'L3'
                   OR EXISTS (SELECT 1 FROM eval.golden_chunks gc
                               WHERE gc.gold_id = g.gold_id AND gc.chunk_id IS NOT NULL))
              AND (%s::text IS NULL OR g.세트 = %s::text)
              AND (%s OR g.평가범위 IS NULL OR g.평가범위 NOT LIKE '범위밖%%')
            ORDER BY g.gold_id""", (세트, 세트, 범위밖포함))
    cols = ("gold_id", "세트", "사업명", "적용범위", "질문", "정답판정", "비목",
            "대상", "평가범위", "채점모드")
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def 사업키(사업명: str | None) -> str | None:
    """🔴 D2 이후의 규약. 공통 문항은 사업명이 NULL 이고 적용범위에 원표기가 있다.

    D2 이전 코드는 '공통(지침 제14차)' 를 사업명으로 넘겨 존재하지 않는 사업으로
    필터링하고 있었다. 방어적으로 여기서 한 번 더 접는다.
    """
    if not 사업명 or 사업명.startswith("공통"):
        return None
    return 사업명


def 요약(run_id: int, *, conn=None) -> dict:
    """적재된 실행 하나를 사람이 읽는 형태로 되읽는다 (검증용)."""
    with db.borrow(conn) as conn:
        cur = conn.cursor()
        cur.execute("SELECT 종류, 시작, 코퍼스버전, git커밋, 문항수, 지표, 설정, 라벨 "
                    "FROM eval.runs WHERE run_id=%s", (run_id,))
        r = cur.fetchone()
        if not r:
            raise LookupError(f"run_id={run_id} 없음")
        cur.execute("SELECT count(*), count(*) FILTER (WHERE 적중) "
                    "FROM eval.run_items WHERE run_id=%s", (run_id,))
        n, hit = cur.fetchone()
        return {"run_id": run_id, "종류": r[0], "시작": r[1], "코퍼스버전": r[2],
                "git커밋": r[3], "문항수": r[4], "지표": r[5], "설정": r[6], "라벨": r[7],
                "items": n, "적중": hit}


if __name__ == "__main__":
    # 스모크 — 실제 표에 1행 넣고 되읽는다. 지표는 빈 dict 라 지표 판독에 섞이지 않는다.
    rid = 기록({"종류": "judge", "설정": {"smoke": True}, "지표": {},
               "라벨": "eval_store 스모크", "비고": "D4 자체 점검"}, [])
    print("run_id =", rid)
    for k, v in 요약(rid).items():
        print(f"  {k:10} {v}")
