# -*- coding: utf-8 -*-
"""D5 — 정답셋 실전 E2E. `orchestrate.판정()` 을 끝까지 돌리고 `eval.runs` 에 남긴다.

**오늘 이게 내는 유일하게 중요한 숫자.**

    판정 단계 격리 (근거를 정답으로 주면)   일치율 84.7% · 치명 오답 0
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

from collections import Counter

import argparse
import hashlib
import json
import os
import re
import sys
import time
import traceback

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import db  # noqa: E402
import eval_store  # noqa: E402

DSN = db.DSN

판정4 = ("가능", "조건부", "불가", "판단불가")

# ════════════════════════════════════════════════════════════════════════════
# 원본 포획 (env `SUDDOE_EVAL_RAW=1` · 기본 off)
# ════════════════════════════════════════════════════════════════════════════
# 🔴 **이건 임시방편이다. 언젠가 orchestrate 에 관측 훅을 다는 게 정답이다.**
#
# run 191(93문항)이 리플레이 불가였던 이유는 eval 의 결손이 아니라 **`orchestrate.판정()`
# 의 반환 계약 결손**이다:
#   · `프롬프트길이` 는 dry 분기(orchestrate.py:625)에만 있고 실전 update(:688~697)에 없다
#     → DB 실측 run 191: 프롬프트길이 non-null 0/93
#   · 검증 «전» LLM 원 JSON(`출력`) · `룰들` · 폐기된 인용·전제는 함수 밖으로 아예 안 나온다
#     (`llm_validate.검증` 은 `(d, 사유)` 만 돌려준다 — 폐기분은 사유 «문자열» 로만 남는다)
#   · `dangling` 은 응답 안에 있지만 eval 이 안 떴다 → run 191 원출력에 `검색` 키 0/93
#
# orchestrate.py 는 백엔드 레인이 도는 본 트리와 같은 파일이라 이 레인이 못 고친다.
# 그래서 **모듈 전역을 감싸 반환 계약을 우회한다.** 소유 밖 파일은 0줄 고친다.
# 성립 근거: orchestrate.py:49~52 가 전부 `from X import Y` 꼴이라 `조립`·`정규화`·
# `검증`·`llm_호출` 이 orchestrate 의 모듈 전역이고 호출도 수식 없이 부른다.
#
# 🔴 **조용한 결손 금지.** 감싸기가 안 먹었으면 경고가 아니라 예외로 죽는다 —
#    빈 칸이 「LLM 이 그걸 안 냈다」로 읽히면 다음 사람이 또 벽을 만난다.
RAW = os.environ.get("SUDDOE_EVAL_RAW") == "1"
RAW_PROMPT = os.environ.get("SUDDOE_EVAL_RAW_PROMPT") == "1"

_포획: dict = {}          # 문항 1건 분량. 매 문항 시작에 비운다
_계수: Counter = Counter()  # run 전체 누계 — 정체성 가드 ③ 의 재료

# 조립 결과는 "\n\n".join(블록) 이고 블록 머리는 `## B<n>. …` 이다
# (assemble_context.원문블록:164 · :183). 첫 머리 앞은 B0(고정 시스템 지시).


class 포획실패(RuntimeError):
    """감싸기가 안 먹었거나 지나간 자취가 안 맞는다. run 을 저장하지 않고 죽는다."""


def _sha(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


def _블록분해(프롬프트: str) -> dict:
    """B0~B6 구간별 자수·sha1.

    🔴 전체 해시 하나로는 「어느 블록이 변했나」를 못 가른다 — P3 의 행분해 전/후
       비교가 정확히 그걸 묻는다.
    🔴 **경계 정의는 여기 두지 않는다.** P3(ai-1e)이 `assemble_context` 에
       확정해 둔 것을 부른다 — 정의가 둘이면 P3 의 표와 이 run 이 안 맞는다.
    """
    from assemble_context import 블록자수, 블록해시
    자수, 해시 = 블록자수(프롬프트), 블록해시(프롬프트)
    # 🔴 P3 이 준 검산: 자수 합 == len(프롬프트). 안 맞으면 조립기 쪽 사고다.
    if sum(자수.values()) != len(프롬프트):
        raise 포획실패(f"블록 자수 합 {sum(자수.values())} != 프롬프트 {len(프롬프트)}")
    return {k: {"자수": v, "sha1": 해시.get(k)} for k, v in 자수.items()}


def 감싸기() -> None:
    """orchestrate 모듈 전역을 기록 래퍼로 바꾼다. `SUDDOE_EVAL_RAW=1` 일 때만 부른다."""
    import assemble_context
    import llm_validate
    import normalize_run
    import orchestrate as orch

    # ── 가드 ① 감싸기 «전» 정체성. import 꼴이 바뀌면 여기서 깨진다 ────────
    쌍 = [(orch, "조립", assemble_context.조립),
          (orch, "정규화", normalize_run.정규화),
          (orch, "검증", llm_validate.검증),
          (orch, "llm_호출", normalize_run.llm_호출),
          (normalize_run, "llm_호출", normalize_run.llm_호출)]
    for m, 이름, 정본 in 쌍:
        if getattr(m, 이름) is not 정본:
            raise 포획실패(
                f"가드①: {m.__name__}.{이름} 이 정본과 다르다. import 꼴이 바뀌었거나 "
                f"이미 누가 감쌌다 — 이 상태로는 포획이 조용히 빈다")

    본_조립, 본_정규화, 본_검증, 본_호출 = (assemble_context.조립, normalize_run.정규화,
                                    llm_validate.검증, normalize_run.llm_호출)

    def w_조립(*a, **kw):
        프롬프트, s맵, 사슬 = 본_조립(*a, **kw)
        rec = {"프롬프트길이": len(프롬프트), "sha1": _sha(프롬프트),
               "블록": _블록분해(프롬프트), "s맵크기": len(s맵),
               "참조사슬수": len(사슬 or []), "변형": kw.get("변형")}
        if RAW_PROMPT:
            rec["프롬프트"] = 프롬프트
        _포획["조립"] = rec
        _계수["조립"] += 1
        return 프롬프트, s맵, 사슬

    def w_정규화(*a, **kw):
        출력, 메타 = 본_정규화(*a, **kw)
        # ① 의 1겹 원본. P1: 「④만으로는 리플레이가 안 닫힌다」
        _포획["①정규화"] = {"출력": 출력, "메타": 메타}
        _계수["①정규화"] += 1
        return 출력, 메타

    def _w_호출(태그, *, 메타통째: bool):
        """🔴 **인자를 남긴다.** run 191 을 못 닫은 이유가 이것이다 — 라벨은
        max_tokens3000 인데 코드는 1500 이었고 어느 쪽이 돌았는지 확인할 수단이 0이었다.
        ①(400)과 ④(1500)은 값이 다르다. 뭉뚱그리지 않고 자리마다 따로 센다.

        `메타통째` 는 ① 자리만 True 다. ④ 의 finish_reason·usage 는 ai-e8 이
        orchestrate 쪽에 직접 넣어 `응답["모델"]` 로 항상 나온다(플래그와 무관하게).
        여기서 또 적으면 같은 값이 두 군데 남아 나중에 어느 쪽이 기준인지 갈린다.
        """
        def f(프롬프트, 스키마=None, **kw):
            _계수[태그] += 1
            출력, 메타 = 본_호출(프롬프트, 스키마, **kw)
            rec = {"프롬프트길이": len(프롬프트), "sha1": _sha(프롬프트),
                   "인자": {k: v for k, v in kw.items() if k != "스키마"},
                   "원출력": 출력}
            if 메타통째:
                rec["메타"] = 메타          # ① 의 종료이유·토큰. ④ 는 orchestrate 가 낸다
            if RAW_PROMPT:
                rec["프롬프트"] = 프롬프트
            _포획.setdefault(태그, []).append(rec)
            return 출력, 메타
        return f

    def w_검증(llm출력, s맵, **kw):
        d, 사유 = 본_검증(llm출력, s맵, **kw)
        전_인용 = [x for x in (llm출력.get("인용") or []) if isinstance(x, str)]
        후_인용 = [c.get("s번호") for c in (d.get("인용목록") or []) if isinstance(c, dict)]
        전_전제 = [(p.get("사실") or "").strip()
                 for p in (llm출력.get("전제") or []) if isinstance(p, dict)]
        후_전제 = [(p.get("사실") or "").strip()
                 for p in (d.get("전제목록") or []) if isinstance(p, dict)]
        전_할일 = [h.get("항목") for h in (llm출력.get("해야할일") or []) if isinstance(h, dict)]
        후_할일 = [h.get("항목") for h in (d.get("해야할일") or []) if isinstance(h, dict)]
        _포획["④검증전"] = llm출력
        _포획["검증"] = {
            # 🔴 폐기분은 `검증()` 안에서 사라지고 사유 문자열만 남는다. 차집합으로 되살린다
            "폐기_인용": [x for x in 전_인용 if x not in 후_인용],
            "폐기_전제": [x for x in 전_전제 if x not in 후_전제],
            "폐기_해야할일": [x for x in 전_할일 if x not in 후_할일],
            "폐기사유": 사유,
            "룰들": kw.get("룰들"),
            "dangling": kw.get("dangling"),
            "체크코드수": len(list(kw.get("체크코드") or [])),
            "프롬프트전달됨": bool(kw.get("프롬프트")),   # 층 B 가 깨어 있었나
            "f사실전달됨": kw.get("f사실") is not None,
        }
        _계수["검증"] += 1
        return d, 사유

    orch.조립 = w_조립
    orch.정규화 = w_정규화
    orch.검증 = w_검증
    # 🔴 두 이름은 같은 함수를 «가리키지만» 서로 다른 변수다. 한쪽만 갈면 다른 쪽이 샌다.
    #    ① 의 실제 호출은 normalize_run.py:280 이 **자기 모듈 전역**을 부르는 것이라
    #    `orchestrate.llm_호출` 만 감싸면 ① 이 통째로 안 잡힌다.
    orch.llm_호출 = _w_호출("④판정LLM", 메타통째=False)
    normalize_run.llm_호출 = _w_호출("①정규화LLM", 메타통째=True)

    # ── 가드 ② 감싼 «후» ────────────────────────────────────────────────
    for m, 이름, 정본 in 쌍:
        if getattr(m, 이름) is 정본:
            raise 포획실패(f"가드②: {m.__name__}.{이름} 이 안 감싸졌다")


def _가드3(items: list[dict], dry: bool) -> None:
    """가드 ③ — 「진짜로 지나갔나」. ①② 는 import 시점만 본다.

    🔴 어긋나면 run 을 **저장하지 않고** 죽는다. 반쯤 빈 run 이 남는 게 제일 나쁘다.

    🔴 ai-e8 의 초안은 「llm_호출 횟수/2」였는데 그건 성립하지 않는다.
       `정규화()` 는 **자기 모듈 전역**의 `llm_호출`(normalize_run.py:280)을 부르므로
       `orchestrate.llm_호출` 을 감싸도 ① 은 안 걸린다. 그래서 두 자리를 따로 감싸고
       (`④판정LLM` · `①정규화LLM`) 여기서 **문항 단위 불변식**으로 센다.
    """
    if not RAW:
        return
    if _계수["조립"] == 0:
        raise 포획실패("가드③: 조립 래퍼가 한 번도 안 걸렸다 — 포획이 통째로 비었다")
    나쁨 = []
    for it in items:
        원 = it["원출력"].get("원본") or {}
        경로 = it["원출력"].get("경로") or ""
        if "4조립" in 경로 and not 원.get("조립"):
            나쁨.append((it["gold_id"], "4조립 을 지났는데 조립 포획이 없다"))
        if "6검증" in 경로:
            if not 원.get("검증"):
                나쁨.append((it["gold_id"], "6검증 을 지났는데 검증 포획이 없다"))
            if not 원.get("④판정LLM"):
                나쁨.append((it["gold_id"], "6검증 을 지났는데 ④ LLM 포획이 없다"))
    # 실전에서 6검증을 지난 문항 수 == ④ 호출 누계 (재시도는 llm_호출 «안» 이라 안 샌다)
    검증문항 = sum(1 for it in items if "6검증" in (it["원출력"].get("경로") or ""))
    if not dry and _계수["④판정LLM"] != 검증문항:
        나쁨.append((None, f"가드③: ④ 호출 누계 {_계수['④판정LLM']} != 6검증 문항 {검증문항}"))
    if 나쁨:
        raise 포획실패("가드③ 위반 " + str(len(나쁨)) + "건: " + str(나쁨[:8]))


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


def _부분집합(이름: str | None, gold_ids: str | None) -> tuple[list[int] | None, str | None]:
    """🔴 부분집합은 **세트 이름이 아니라 gold_id 로 고정한다.**
    세트별로 빠지는 문항 수가 달라 이름으로 부르면 run 마다 다른 집합이 잡히고,
    튜닝분과 held-out 이 조용히 섞인다. 기준 파일은 scratchpad/P4_부분집합_0903.json.
    """
    if gold_ids:
        return [int(x) for x in re.split(r"[,\s]+", gold_ids.strip()) if x], "직접지정"
    if not 이름:
        return None, None
    경로 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "scratchpad", "P4_부분집합_0903.json")
    with open(경로, encoding="utf-8") as f:
        표 = json.load(f)
    if 이름 not in 표:
        sys.exit(f"부분집합 '{이름}' 이 {경로} 에 없다. 있는 것: "
                 f"{[k for k in 표 if isinstance(표[k], list)]}")
    return list(표[이름]), f"{이름}@P4_부분집합_0903.json(기준run={표.get('기준run')})"


def 실행(*, dry: bool, limit: int | None, 세트: list[str] | None, 라벨: str | None,
        top_k: int, 기록: bool, 변형: str = "V0", 부분집합: str | None = None,
        gold_ids: str | None = None, max_model_len: int = 24576) -> int:
    ids, 부분집합표기 = _부분집합(부분집합, gold_ids)
    with psycopg.connect(DSN) as conn:
        cur = conn.cursor()

        # 세트가 정확히 1개면 기존 SQL 경로 그대로 탄다 (현행과 바이트 단위 동일).
        # 여러 개면 SQL 은 전체로 두고 파이썬에서 거른다 — eval_store 는 P4 소유가 아니다.
        문항 = eval_store.평가대상(cur, 세트=(세트[0] if 세트 and len(세트) == 1 else None))
        if 세트 and len(세트) > 1:
            문항 = [m for m in 문항 if m["세트"] in set(세트)]
        if ids:
            있는 = {m["gold_id"] for m in 문항}
            빠짐 = [g for g in ids if g not in 있는]
            if 빠짐:
                # 🔴 조용히 빼지 않는다. 분모가 흔들리면 튜닝/held-out 대조가 무너진다
                sys.exit(f"--부분집합/--gold-ids 의 {len(빠짐)}건이 평가대상에 없다: {빠짐[:10]}")
            문항 = [m for m in 문항 if m["gold_id"] in set(ids)]
        if limit:
            문항 = 문항[:limit]
        if not 문항:
            sys.exit("평가 대상이 0건이다. scripts/pin_golden_chunks.py 를 먼저 돌려라.")

        # 비교 앵커 재료. `with` 밖(집계 절)에서는 conn 이 닫혀 있어 여기서 미리 읽는다.
        코퍼스버전값 = eval_store.코퍼스버전(cur)
        cur.execute("SELECT count(*), count(*) FILTER (WHERE verified) FROM corpus.rules")
        rules수 = dict(zip(("총", "verified"), cur.fetchone()))
        cur.execute("SELECT 적용대상, count(*) FROM corpus.chunks GROUP BY 1 ORDER BY 2 DESC")
        적용대상분포 = {r[0]: r[1] for r in cur.fetchall()}

        # 고정 정답 좌표를 미리 읽는다 — 문항마다 재계산하지 않는다(결정성).
        정답청크: dict[int, set[int]] = {}
        정답좌표: dict[int, set[tuple]] = {}
        for m in 문항:
            gid = m["gold_id"]
            정답청크[gid] = eval_store.정답청크(cur, gid)
            # 🔴 정답 좌표는 `golden_chunks.조번호` 가 아니라 **그 청크의 좌표**로 잡는다.
            #    golden_chunks.조번호 는 정답셋 원표기('제20조①' · '[붙임2] 외주용역비
            #    유의사항')를 그대로 보존한 것이라 인용 쪽 표기('제20조' · '붙임2')와 다르다.
            #    실측: 104행 중 85행이 형식 불일치 → 그대로 비교하면 **인용적중이 허위 0%** 로
            #    나온다. chunk_id 는 이미 고정돼 있으니 corpus.chunks 에서 좌표를 읽으면
            #    표기가 자동으로 맞춰지고 기준 문서가 한 곳(chunks)으로 유지된다.
            cur.execute("SELECT c.doc_id, c.조번호 FROM eval.golden_chunks gc "
                        "JOIN corpus.chunks c ON c.chunk_id = gc.chunk_id "
                        "WHERE gc.gold_id = %s", (gid,))
            정답좌표[gid] = {(r[0], r[1]) for r in cur.fetchall()}

        print(f"E2E {'드라이런' if dry else '실전'} · {len(문항)}문항 "
              f"(세트={','.join(세트) if 세트 else '전체'}"
              f"{' · 부분집합=' + 부분집합표기 if 부분집합표기 else ''}) · "
              f"top_k={top_k} · 변형={변형} · 원본포획={'on' if RAW else 'off'}"
              f"{'(프롬프트 전문 포함)' if RAW and RAW_PROMPT else ''}\n")

        import orchestrate  # 여기서 import 한다 — --help 만 볼 때 모델을 안 올리게
        if RAW:
            감싸기()

        items: list[dict] = []
        오류: list[tuple[int, str]] = []
        t0 = time.time()

        for i, m in enumerate(문항, 1):
            gid = m["gold_id"]
            사업 = eval_store.사업키(m["사업명"])
            _포획.clear()          # 문항 단위 리셋. LLM 호출은 전부 메인 스레드다
            try:
                r = orchestrate.판정(m["질문"], 사업명=사업, dry=dry, top_k=top_k,
                                    conn=conn, 기록=False, 변형=변형)
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
                    # 🔴 `orchestrate.판정()` 은 **dry 분기에서만** 이걸 채운다 —
                    #    실전 update 에는 이 키가 없어서 run 191 은 93/93 None 이었다.
                    #    실전에서는 조립 래퍼가 잡은 값으로 메운다(`SUDDOE_EVAL_RAW=1`).
                    #    `--max-model-len` 이 문항 집합에 맞는지 재는 유일한 실측치다
                    #    (프롬프트가 넘치면 맨 뒤 B6=질문이 잘린다).
                    "프롬프트길이": (r.get("프롬프트길이")
                                or (_포획.get("조립") or {}).get("프롬프트길이")),
                    "지연ms": r.get("지연ms") or {},
                    "s맵": r.get("s맵") or {},
                    # 🔴 아래 셋은 **플래그와 무관하게 항상** 남긴다 (원출력 키가 3개 는다).
                    #    `모델.종료이유`(finish_reason)는 «판단불가가 모델의 선택인가
                    #    잘림인가»를 가르는 값이라 플래그 뒤에 두면 안 된다 — 플래그를 안 켠
                    #    run 이 또 나오면 run 191 과 똑같이 못 닫힌다.
                    #    `dangling` 은 run 191 원출력에 `검색` 키가 0/93 이라 통째로 없었다.
                    "모델": r.get("모델") or {},
                    "dangling": (검색.get("dangling") or []),
                    "후보수": 검색.get("후보수"),
                    # 🔴 P2 요청. orchestrate:688·554 가 응답에 이미 넣어 둔 것을
                    #    eval 이 안 베끼고 있었을 뿐이다 — 문항당 200바이트 안팎.
                    #    run 191 은 `기록=False` 라 tenant.decisions 에도 안 남아
                    #    **실제 LLM 품목·용도가 어디에도 없다.** 이게 없으면 P2 의
                    #    금지예시·비목 축은 GPU 를 다시 켜야만 열린다.
                    "정규화": r.get("정규화"),
                    "비목": r.get("비목"),
                    "게이트": r.get("게이트"),
                    "금지근거": r.get("금지근거"),      # 게이트 A 일 때만 찬다
                    # 검증 «전» 1겹 원본 일습. `SUDDOE_EVAL_RAW=1` 일 때만 찬다.
                    **({"원본": dict(_포획)} if RAW else {}),
                    # 채점 재료. 나중에 재채점할 때 다시 안 돌려도 되게 같이 남긴다
                    "top5": top5,
                    "근거적중": bool(set(top5) & 정답청크[gid]),
                    "인용적중": bool(_인용좌표(r) & 정답좌표[gid]),
                    "치명": _치명(정답, 예측),
                    # 실패단계가 있거나 경로에 '실패'/'예외'가 박혔으면 §8 실패 경로가 닫은 것이다.
                    # 'dry중단' 도 모델의 선택이 아니다 — LLM 을 아예 안 불렀다.
                    # 이 셋 중 아무것도 아닌 판단불가만 "모델이 스스로 고른 판단불가" 다.
                    "실패경로": bool(r.get("실패단계")
                                  or any(k in (r.get("경로") or "")
                                         for k in ("실패", "예외", "dry"))),
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
        # 🔴 **다수결 기준선을 같이 낸다** (2026-09-01. 오너 지시 «정답셋 성역을 깨라»).
        #    정답셋 정답이 «불가» 로 쏠려 있어(채점 62문항 기준 66.1%) 일치율 한 숫자만
        #    보면 실력인지 쏠림인지 갈리지 않는다. **상수 예측기('항상 불가')가 몇 점인지**를
        #    같은 분모로 찍어 두면, 지표가 기준선을 못 넘었을 때 그 사실이 표에서 바로 보인다.
        #    `초과`(일치율 - 기준선)가 이 평가의 진짜 신호다. 양수가 아니면 개선이 아니다.
        정답분포 = Counter(it["정답"] for it in 부분)
        인용분 = [it for it in 부분 if it["정답"] != "판단불가"]
        기준선 = (정답분포.most_common(1)[0][1] / n * 100) if 부분 else 0.0
        일치 = sum(it["적중"] for it in 부분) / n * 100
        return {
            "문항수": len(부분),
            "일치율": round(일치, 1),
            "다수결기준선": round(기준선, 1),
            "기준선초과": round(일치 - 기준선, 1),
            "최빈정답": 정답분포.most_common(1)[0][0] if 부분 else None,
            "정답분포": dict(정답분포),
            "치명오답": sum(it["원출력"]["치명"] for it in 부분),
            "판단불가율": round(sum(it["예측"] == "판단불가" for it in 부분) / n * 100, 1),
            # 🔴 판단불가를 한 숫자로 세면 안 된다 (2026-09-01 실전 E2E 에서 드러난 결함).
            #    §7 은 "판단불가율 0% 면 근거 없이 답을 만들고 있다는 뜻" 이라고 경고하는데,
            #    그 경고가 풀렸는지는 **모델이 스스로 골랐는가**로만 판정된다.
            #    실전 1차(run_id=189)에서 판단불가 5건이 전부 max_tokens=1500 잘림
            #    (§8 실패 경로)이었다 — 6.5% 만 보면 경고가 풀린 것처럼 보이지만
            #    모델이 스스로 고른 횟수는 0 이었다. 두 개를 갈라서 센다.
            "판단불가_모델선택": sum(it["예측"] == "판단불가" and not it["원출력"]["실패경로"]
                                for it in 부분),
            "판단불가_실패경로": sum(it["예측"] == "판단불가" and it["원출력"]["실패경로"]
                                for it in 부분),
            # 🔴 정답이 «판단불가» 인 문항은 근거·인용 채점에서 뺀다 (2026-09-01).
            #    규범에 답이 없다는 것이 정답이라 **고정할 정답 청크가 없다** — 분모에
            #    두면 맞힐 수 없는 문항이 섞여 인용적중률이 구조적으로 깎인다.
            #    판정일치율에는 그대로 든다. 분모가 다르므로 따로 찍는다.
            "인용채점분모": len(인용분),
            "근거적중률": (round(sum(it["원출력"]["근거적중"] for it in 인용분)
                              / len(인용분) * 100, 1) if 인용분 else None),
            "인용적중률": (round(sum(it["원출력"]["인용적중"] for it in 인용분)
                              / len(인용분) * 100, 1) if 인용분 else None),
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
    # 🔴 프롬프트 길이 분포 — `pod_serve.sh --max-model-len` 의 근거다 (2026-09-01).
    #    24576 은 **구 77문항 실측**으로 잡은 값이라 문항이 늘면 다시 재야 한다.
    #    넘치면 에러가 아니라 **앞이 잘린 채 조용히 답한다** — 질문이 사라진 오답이다.
    길이 = sorted(x for x in (it["원출력"].get("프롬프트길이") for it in items) if x)
    if 길이:
        # 🔴 MML 이 24576 으로 **하드코딩**돼 있었다. run 191 은 40960 으로 떴는데
        #    이 줄은 24576 기준으로 「초과 14건」을 찍는다 — 표와 서버가 다른 수를 본다.
        #    이제 `--max-model-len` 으로 받고 `설정` 에 박는다. 기본값은 24576 그대로라
        #    안 주면 현행과 같은 줄이 나온다.
        MML, 자당토큰 = max_model_len, 0.7   # 한국어 법령문 0.6~0.7 토큰/자 (보수적으로 상단)
        여유 = int((MML - 3000) / 자당토큰)  # max_tokens=3000 을 뺀 입력 예산(자)
        넘 = [x for x in 길이 if x > 여유]
        p90 = 길이[min(len(길이) - 1, int(len(길이) * 0.9))]
        print(f"프롬프트 자수  중앙 {길이[len(길이)//2]:,} · p90 {p90:,} · "
              f"최장 {길이[-1]:,} · n={len(길이)}")
        print(f"  입력 예산 {여유:,}자 (max-model-len {MML:,} - max_tokens 3,000, {자당토큰}토큰/자)"
              f" -> 초과 {len(넘)}건" + ("  🔴 --max-model-len 을 올려라" if 넘 else "  OK"))
    머리 = (f"{'구간':12}{'문항':>5}{'일치율':>9}{'기준선':>8}{'초과':>8}{'치명':>6}"
            f"{'판단불가(모델)':>17}{'근거적중':>10}{'인용적중':>10}")
    print(머리)
    print("-" * len(머리))

    def 줄(이름, g):
        근거 = f"{g['근거적중률']:9.1f}%" if g["근거적중률"] is not None else f"{'미측정':>9} "
        인용 = f"{g['인용적중률']:9.1f}%" if g["인용적중률"] is not None else f"{'미측정':>9} "
        불가 = f"{g['판단불가율']:.1f}%(모델{g['판단불가_모델선택']})"
        초과 = f"{g['기준선초과']:+.1f}"
        print(f"{이름:12}{g['문항수']:5}{g['일치율']:8.1f}%{g['다수결기준선']:7.1f}%{초과:>8}"
              f"{g['치명오답']:6}{불가:>17}{근거}{인용}")

    줄("전체", 전체)
    if 전체["기준선초과"] <= 0:
        print(f"\n🔴 일치율이 다수결 기준선({전체['다수결기준선']}%, 항상 "
              f"'{전체['최빈정답']}')을 넘지 못했다. 이 수치는 판정 실력의 증거가 아니다.\n"
              f"   정답 분포: {전체['정답분포']}")
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
    if not dry:
        모델선택 = 전체["판단불가_모델선택"]
        실패경로 = 전체["판단불가_실패경로"]
        print(f"\n판단불가 {모델선택 + 실패경로}건 = 모델 선택 {모델선택} + §8 실패 경로 {실패경로}")
        if 모델선택 == 0:
            print("🔴 **모델이 스스로 판단불가를 고른 횟수가 0 이다.** 계약 §7 의 경고는 "
                  "아직 해소되지 않았다 — 실패 경로가 닫은 건수는 이 경고를 풀어주지 않는다.")
            if 실패경로:
                print(f"   (판단불가 {실패경로}건은 전부 §8 실패 경로다. 사고를 안전하게 닫은 "
                      "결과일 뿐 '근거가 없으면 답하지 않는다' 의 증거가 아니다.)")

    # ── 적재 ────────────────────────────────────────────────────────────
    # 🔴 가드 ③ — **저장 «전»에** 죽는다. 반쯤 빈 run 이 남는 게 제일 나쁘다.
    _가드3(items, dry)

    # 🔴 run 191 의 `설정` 은 {dry, limit, top_k, 세트, 채점, 정답고정} 뿐이었다.
    #    변형·max-model-len·max_tokens·GPU·코퍼스버전·켜진 플래그가 하나도 없어
    #    「이 숫자가 무엇 때문에 나왔나」를 답할 수 없다. 전부 박는다.
    #    ① 의 최대토큰은 400, ④ 는 1500 이다(자리마다 다르다) — 실제로 나간 값을
    #    래퍼가 잰 대로 적고, 안 켰으면 None 으로 둔다. 주장하지 않는다.
    def _관측토큰(태그):
        vals = {v for it in items
                for v in [((it["원출력"].get("원본") or {}).get(태그) or [{}])[0]
                          .get("인자", {}).get("최대토큰")] if v is not None}
        return sorted(vals) or None

    설정 = {"dry": dry, "top_k": top_k, "세트": 세트, "limit": limit,
          "정답고정": "eval.golden_chunks(D3)",
          "채점": "결정론 4-way + 치명오답 + 근거/인용 적중",
          "변형": 변형,
          "부분집합": 부분집합표기,
          "문항수": len(items),
          "max_model_len": max_model_len,
          "관측_최대토큰_①": _관측토큰("①정규화LLM"),
          "관측_최대토큰_④": _관측토큰("④판정LLM"),
          "원본포획": RAW, "프롬프트전문": RAW and RAW_PROMPT,
          # 코퍼스버전은 컬럼에도 들어가지만 설정에도 박는다 — 표만 보고도 갈리게
          "코퍼스버전": 코퍼스버전값,
          # 🔴 코퍼스버전 해시는 (chunks, embedding, refs, documents, max chunk_id) 만
          #    본다. `corpus.rules` 와 `chunks.적용대상` 이 바뀌어도 해시는 그대로다 —
          #    그 둘은 검색 필터와 룰 경로의 입력이라 판정을 바꾼다. 따로 센다.
          "rules수": rules수, "적용대상분포": 적용대상분포,
          "GPU": os.environ.get("SUDDOE_GPU"),
          "VLLM_URL": os.environ.get("VLLM_URL"),
          "VLLM_MODEL": os.environ.get("VLLM_MODEL"),
          "RUNPOD_POD_ID": os.environ.get("RUNPOD_POD_ID"),
          # 🔴 P2·P3 가 켜는 플래그가 무엇이었는지 남는다. 안 남기면 다음 run 과 못 뺀다
          "SUDDOE_플래그": {k: v for k, v in sorted(os.environ.items())
                        if k.startswith("SUDDOE_")}}
    run_id = None
    if 기록:
        run_id = eval_store.기록(
            {"종류": "e2e",
             "설정": 설정,
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
    # 🔴 choices 를 손으로 적으면 세트가 늘 때마다 조용히 못 고르게 된다
    #    (실측: '공식'·'보강' 이 추가됐는데 목록은 2개로 굳어 있었다).
    # 🔴 choices 를 손으로 적으면 세트가 늘 때마다 조용히 못 고르게 된다 (위 주석).
    #    nargs='+' 로 여러 세트를 한 run 에 담는다 — 1개만 주면 기존 SQL 경로 그대로다.
    ap.add_argument("--세트", nargs="+", choices=["본세트", "적대적", "공식", "보강"])
    ap.add_argument("--top-k", type=int, default=5, dest="top_k")
    ap.add_argument("--변형", default="V0",
                    help="A12 프롬프트 변형. V0=기준선 · V1~V6 (assemble_context.변형들)")
    # 🔴 부분집합은 gold_id 로 고정한다. 세트 이름으로 다시 뽑지 마라 — 이름이 흔들리면
    #    튜닝분과 held-out 이 섞인다.
    ap.add_argument("--부분집합", choices=["튜닝52", "미사용41"],
                    help="scratchpad/P4_부분집합_0903.json 의 gold_id 목록")
    ap.add_argument("--gold-ids", dest="gold_ids", help="쉼표구분 gold_id 직접 지정")
    ap.add_argument("--max-model-len", type=int, default=24576, dest="max_model_len",
                    help="프롬프트 예산 계산·설정 기록용. 서버 기동값과 같게 줘라 "
                         "(run 191 은 40960 이었는데 이 계산은 24576 로 굳어 있었다)")
    ap.add_argument("--라벨")
    ap.add_argument("--no-log", action="store_true", help="eval.runs 에 남기지 않는다")
    a = ap.parse_args()
    실행(dry=a.dry, limit=a.limit, 세트=a.세트, 라벨=a.라벨,
       top_k=a.top_k, 기록=not a.no_log, 변형=a.변형, 부분집합=a.부분집합,
       gold_ids=a.gold_ids, max_model_len=a.max_model_len)


if __name__ == "__main__":
    main()
