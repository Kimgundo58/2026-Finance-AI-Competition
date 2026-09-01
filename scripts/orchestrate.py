# -*- coding: utf-8 -*-
"""판정 오케스트레이터 — (1)~(7) 을 코드가 지휘한다.

기준 문서: `docs/6_LLM/` (호출 설계·출력 스키마·평가).
동결 인터페이스는 `docs/기록/2026-08-31.md` 의 세션 계약분.

## 🔴 오케스트레이터는 LLM 이 아니다
에이전트가 다음 행동을 고르지 않는다. 순서가 코드에 박혀 있어서 호출 수가 고정되고,
지연이 예측되고, 같은 입력에 같은 출력이 나온다. 이 도메인에서 재현성은 편의가 아니라
요건이다 — 판정 이력을 나중에 설명해야 한다.

LLM 호출은 **2회 고정**이다. (1) 정규화 · (4) 판정 조립. 그 사이의 검색·룰 조회·금액
비교·효력 결정·전제 해소·검증은 전부 코드다.

## 남의 모듈이 없으면 스텁으로 간다
`# STUB: <세션>` 이 붙은 것은 합류점에서 교체된다. 스텁이 있어도 **배관은 진짜로 돈다** —
특히 검색 스텁은 `eval_retrieval.py` 의 실제 SQL 을 그대로 쓴다. 남의 모듈을 기다리며
멈추면 오늘 밤이 끝난다.

## 게이트 4갈래 (`docs/6_LLM/6-1_호출_설계.md`)
    A 금지목록 적중    (2) 에서 즉답 "불가"            LLM 0회
    B 검색 스코어 미달  게이트에서 판단불가             LLM 1회
    C 비목 신뢰도 갈림  두 경로를 모두 판정해 나란히    LLM 2배
    D 정상             (4) 까지                        LLM 2회

## 🔴 모든 실패의 기본값은 판단불가
DB 끊김 · 타임아웃 · 스키마 위반 · 검색 0건 · 인용 검증 실패 — 전부 판단불가로 닫는다.
"아마 가능"은 어떤 경로로도 만들지 않는다. `--fault` 로 전수 검증한다 (A10).

실행:
    PYTHONIOENCODING=utf-8 python scripts/orchestrate.py --q "맥북 250만원" --dry
    PYTHONIOENCODING=utf-8 python scripts/orchestrate.py --golden --dry --limit 5
    PYTHONIOENCODING=utf-8 python scripts/orchestrate.py --fault all --dry
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import db, paths                                           # noqa: E402
paths.ensure_on_path()
from assemble_context import 조립                                    # noqa: E402
from llm_schema import 판정_스키마, 체크코드_enum                      # noqa: E402
from llm_validate import 검증, f_경로집합                              # noqa: E402
from normalize_run import LLM실패, llm_호출, 정규화                     # noqa: E402

DSN = db.DSN

# ════════════════════════════════════════════════════════════════════════════
# 임계치 — 🔴 전부 미결 항목이다 (`docs/9_미결.md` 임계치 목록 #7)
# ════════════════════════════════════════════════════════════════════════════
# 게이트 B. dense 코사인 **최고값** 기준이다 — RRF 점수는 스케일이 없어 임계를 못 건다.
#
# 🔴 **0.0 으로 확정한다 (2026-09-01 실측). 임계를 걸 수 없다는 것이 결론이다.**
#    정답셋 74문항(D3 `golden_chunks` 고정)에서 hit/miss 의 게이트값 분포를 재보니
#    **거의 완전히 겹친다**:
#        hit  min 0.443  p25 0.549  med 0.594  p75 0.656  max 0.725   (41건)
#        miss min 0.467  p25 0.514  med 0.551  p75 0.615  max 0.718   (33건)
#    분리 임계가 존재하지 않는다. 0.52 로 걸면 12건을 차단하는데 그중 3건이 hit 이고,
#    남은 24건의 miss 는 그대로 통과한다 — **맞는 근거를 버리면서 틀린 근거를 못 막는다.**
#    즉 dense 코사인 최고값은 이 규정 모음에서 "근거를 찾았는가" 의 대리변수가 아니다.
#    게이트 B 는 `top5 == 0건` 일 때만 발화한다.
#
#    이건 튜닝 실패가 아니라 측정 결과다. 판단불가는 검색 스코어가 아니라 **(6) 인용
#    검증**이 만들어야 한다는 뜻이고, 그쪽은 이미 촘촘하다(NO_CITATION·CITE_NOT_IN_MAP).
#    분리 신호를 다시 찾는다면 후보는 top1 코사인이 아니라 top1−top5 격차나
#    BM25·dense 합의도다 — 오늘은 재지 않는다 (§10 "오늘은 부품을 잇는 날").
게이트B_임계 = float(os.environ.get("SUDDOE_GATE_B", "0.0"))

# 게이트 C. 비목 후보 1·2위가 이만큼 안 벌어지면 갈렸다고 본다.
#   근거: `item_alias` 0행이라 벡터 경로가 아직 미측정이다. 0.15 는 측정 전 가정이고
#   `docs/기록/2026-08-31_축별보고.md` 에 가정으로 적는다. 갈리면 손해가 "LLM 2배" 뿐이라 관대하게 잡는다.
게이트C_격차 = float(os.environ.get("SUDDOE_GATE_C", "0.15"))
게이트C_최소신뢰 = 0.35

# ════════════════════════════════════════════════════════════════════════════
# 강등코드 18종 — A 가 발행한다 (동결 인터페이스)
# ════════════════════════════════════════════════════════════════════════════
강등코드_전체: tuple[str, ...] = (
    "INVALID_JUDGMENT", "CITE_NOT_IN_MAP", "CITE_DB_MISSING", "CITE_HANG_MISMATCH",
    "PREMISE_NO_BASIS", "PREMISE_BASIS_NOT_IN_MAP", "PREMISE_ENUM", "PREMISE_UNMAPPED",
    "NO_CITATION", "VLM_DOWNGRADE", "B_GRADE_DOWNGRADE", "UNVERIFIED_RULE",
    "TASK_CODE_INVALID", "L3_ONLY_DOWNGRADE", "TENANT_LEAK", "DANGLING_WARN",
    "DOMAIN_WARN", "PRECEDENCE_FLIP",
)


# ════════════════════════════════════════════════════════════════════════════
# 남의 모듈 — 있으면 쓰고 없으면 스텁
# ════════════════════════════════════════════════════════════════════════════
def _옵션임포트(모듈: str, 이름: str) -> Optional[Callable]:
    try:
        m = __import__(모듈)
        return getattr(m, 이름, None)
    except Exception:
        return None


_C_검색 = _옵션임포트("retrieve", "검색")                       # STUB: C
_B_비목확정 = _옵션임포트("rule_lookup", "비목확정")             # STUB: B
_B_금지적중 = _옵션임포트("rule_lookup", "금지적중")             # STUB: B
_B_effective = _옵션임포트("rule_lookup", "effective_rule")     # STUB: B
_B_게이팅 = _옵션임포트("rule_lookup", "l3_게이팅")              # STUB: B
_E_로드 = _옵션임포트("l3_load", "로드")                         # STUB: E
_E_l3룰 = _옵션임포트("l3_load", "l3룰")                         # STUB: E
_F_사례 = _옵션임포트("case_search", "유사사례")                  # STUB: F

모듈상태 = {"C": bool(_C_검색), "B": bool(_B_effective), "E": bool(_E_로드), "F": bool(_F_사례)}


def 워밍업() -> None:
    """프로세스당 1회. 임베딩 모델·kiwi 로드 28초를 첫 판정 밖으로 뺀다 (C5).

    `docs/6_LLM/6-1_호출_설계.md` vLLM 운영 규칙의 "기동 후 더미 1회" 와 같은 이유다 — 첫 요청이
    콜드 스타트를 뒤집어쓰면 지연 예산 측정이 전부 거짓말이 된다.
    """
    try:
        import retrieve
        retrieve.워밍업()
    except Exception as e:
        print(f"⚠️ 검색 워밍업 실패(계속 진행): {type(e).__name__}: {e}", file=sys.stderr)


# ── C 스텁: eval_retrieval.py 의 실제 SQL 을 그대로 쓴다 ─────────────────────
# 진짜로 검색한다. 스텁이라도 배관 검증이 의미를 가지려면 B2 블록에 실제 조문이 와야 한다.
# 폐포·참조사슬·dangling 은 C2·C3 의 몫이라 여기서는 빈 값이다 — 그 자리가 비면
# B3 블록이 빠지고 DANGLING_WARN 이 안 뜬다는 사실 자체가 합류점 1 의 검증 항목이다.
_임베더 = None


def _임베딩(질문: str):
    global _임베더
    if _임베더 is None:
        from sentence_transformers import SentenceTransformer
        _임베더 = SentenceTransformer("nlpai-lab/KURE-v1", device="cpu")
        _임베더.max_seq_length = 1024
    return _임베더.encode([질문], normalize_embeddings=True, convert_to_numpy=True)[0]


def _스텁_검색(cur, 질문: str, 사업명: str | None, *, top_k: int = 5,
              사업필터: bool = False) -> dict:
    from eval_retrieval import BM25, DENSE, rrf
    from stage2_bm25 import 토큰화
    v = _임베딩(질문)
    vec = "[" + ",".join(f"{x:.6f}" for x in v) + "]"
    cur.execute(DENSE, (vec, 50))
    dense = [r[0] for r in cur.fetchall()]
    cur.execute(BM25, {"terms": 토큰화([질문])[0], "k": 50})
    bm = [r[0] for r in cur.fetchall()]
    # 게이트값 = dense 코사인 **최고값**. `1 - 거리` 다 (pgvector `<=>` 는 코사인 거리)
    게이트값 = 0.0
    if dense:
        cur.execute("SELECT 1 - (embedding <=> %s::extensions.vector(1024)) "
                    "FROM corpus.chunks WHERE chunk_id=%s", (vec, dense[0]))
        게이트값 = float(cur.fetchone()[0])
    return {"top5": rrf([dense, bm])[:top_k], "폐포": [], "참조사슬": [],
            "게이트값": 게이트값, "dangling": [], "후보수": len(set(dense) | set(bm)),
            "_출처": "STUB:A(eval_retrieval SQL)"}


# 🔴 2026-08-31 A 결정: 판정 경로는 **사업 필터를 켠다** (C7).
#
# 이건 지표 튜닝이 아니라 인덱스 경계다. 필터를 끄면 예비창업패키지 질문의 top-5 에
# 재도전성공패키지 제21조·창업도약패키지 제32조·모두의 창업 제35조가 들어온다 —
# **남의 사업 규정이 인용 가능한 근거로 B2 블록에 실린다.** `CLAUDE.md` 검색 대상의
# 경계가 "남의 규정이 인용되는 순간 그 자체가 오답" 이라고 못박은 그 상황이다.
# 지표가 올라서 켜는 게 아니다. 지표가 내려가도 켜야 하는 종류의 것이다.
# (마침 C 의 짝지어 비교에서 15개 지표 전부 상승·하락 0 이었다. 상충이 없어 다행일 뿐이다)
#
# `retrieve.사업필터_기본` 은 **False 로 둔 채**로 호출부에서만 켠다. 그래야
# `eval_retrieval.py` 회귀 기준선 52.9% 가 손대지 않은 채 남는다 (§7 동일 조건 비교).
사업필터 = os.environ.get("SUDDOE_사업필터", "1") != "0"


def _검색(cur, 질문, 사업명, *, top_k=5) -> dict:
    try:
        r = (_C_검색 or _스텁_검색)(cur, 질문, 사업명, top_k=top_k, 사업필터=사업필터)
    except TypeError:
        r = (_C_검색 or _스텁_검색)(cur, 질문, 사업명, top_k=top_k)   # 스텁 호환
    # 🔴 계약 방어: 0건이어도 None 금지 — 빈 리스트다. 남의 모듈을 믿지 않는다.
    for k, 기본 in (("top5", []), ("폐포", []), ("참조사슬", []), ("dangling", [])):
        if r.get(k) is None:
            r[k] = 기본
    r.setdefault("게이트값", 0.0)
    r.setdefault("후보수", 0)
    return r


def _비목확정(cur, 품목, 사업명) -> list[dict]:
    if _B_비목확정:
        return _B_비목확정(cur, 품목, 사업명) or []
    return []                                                    # STUB: B


def _금지적중(cur, 품목, 용도, 사업명, 비목):
    return _B_금지적중(cur, 품목, 용도, 사업명, 비목) if _B_금지적중 else None   # STUB: B


def _effective(cur, 사업명, 비목, 기관ID=None):
    return _B_effective(cur, 사업명, 비목, 기관ID) if _B_effective else None    # STUB: B


def _게이팅(l3룰) -> dict:
    if _B_게이팅:
        return _B_게이팅(l3룰)
    # STUB: B — `Agent.md` §3-2 그대로. (4) L3 "가능" → need_upper 강제가 핵심이다.
    if not l3룰:
        return {"need_upper": True, "seed_refs": []}
    if l3룰.get("참조만"):
        return {"need_upper": True, "seed_refs": list(l3룰.get("근거") or [])}
    if l3룰.get("허용") in ("불가", "조건부"):
        return {"need_upper": False, "seed_refs": []}
    return {"need_upper": True, "seed_refs": []}          # 허용='가능' → 🔴 강제


def _l3로드(cur, org_id, 사업명) -> list[dict]:
    if _E_로드 and org_id:
        return _E_로드(cur, org_id, 사업명) or []
    return []                                                    # STUB: E


def _l3룰(cur, org_id, 비목):
    return _E_l3룰(cur, org_id, 비목) if (_E_l3룰 and org_id) else None          # STUB: E


# ════════════════════════════════════════════════════════════════════════════
# (2)-e·B4 — 룰 결과를 문장으로. 🔴 원시 한도값을 프롬프트에 넣지 않는다
# ════════════════════════════════════════════════════════════════════════════
def b4_문장(룰: dict | None) -> str | None:
    """B4 블록 본문. B 의 `B4문장` 이 있으면 그것이 기준이다 (동결 인터페이스).

    없으면 A 가 최소한만 만든다 — **비교가 끝난 문장**이어야 한다.
    LLM 에게 "한도 500만원" 을 주면 계산을 시키는 것이고, 그 순간 (2)-e 가 무의미해진다.
    """
    if not 룰:
        return None
    if 룰.get("B4문장"):
        return str(룰["B4문장"])
    줄 = [f"이 지출에 적용되는 규범은 {룰.get('적용층') or '?'} 층이다."]
    if 룰.get("우선규범"):
        줄.append(f"상위 규범: {룰['우선규범']}")
    줄.append(f"규범상 허용 여부: {룰.get('허용') or '미상'}")
    비교 = 룰.get("금액비교")
    if 비교:
        초과 = 비교.get("초과")
        줄.append("금액 비교 결과: " + ("한도를 초과했다" if 초과 is True else
                                   "한도 내다" if 초과 is False else
                                   f"비교 불가 — {비교.get('사유') or '기준값 없음'}"))
    if 룰.get("사전승인"):
        줄.append("사전승인이 필요하다.")
    if 룰.get("증빙"):
        줄.append("필요 증빙: " + ", ".join(map(str, 룰["증빙"])))
    if not 룰.get("verified"):
        줄.append("(이 룰은 아직 검수 전이다. 단독으로 '가능' 의 근거가 되지 못한다.)")
    return "\n".join(줄)


def b5_문장(cur, org_id) -> str | None:
    """B5 F 요약. 🔴 현물은 없다 — 계상은 지출이 아니다 (오늘 DROP, 계약서 §2)."""
    if not org_id:
        return None
    try:
        r = cur.execute("""SELECT 협약총액, 정부지원_현금, 자기부담_현금
                             FROM tenant.f_profile WHERE org_id=%s LIMIT 1""",
                        (org_id,)).fetchone()
    except Exception:
        return None
    if not r:
        return None
    이름 = ("협약총액", "정부지원(현금)", "자기부담(현금)")
    return "\n".join(f"{n}: {v if v is not None else '미입력'}" for n, v in zip(이름, r))

def b5_값(cur, org_id) -> dict | None:
    """B5 의 **값**. `b5_문장` 의 문자열판이 아니라 층 B 대조용 원본이다 (ai-ba 패치).

    🔴 반환값 셋을 갈라야 한다 — 뭉치면 게스트 가드가 조용히 꺼진다.
        None   조회 자체를 못 했다(예외) = 모른다  -> 층 B 상태 규칙 무발효
        {}     F축이 없다(게스트·미등록·전 NULL)   -> 최대 강도
        {...}  실제 값

    `b5_문장` 은 셋을 전부 None 으로 뭉갠다 — 프롬프트에 넣을 문자열이라 그래도 됐지만,
    검증기는 «모른다» 와 «없다» 를 갈라야 하므로 함수를 합치지 않는다.
    """
    if not org_id:
        return {}                       # 게스트. '모른다' 가 아니라 '없다' 다
    try:
        r = cur.execute("""SELECT 협약총액, 정부지원_현금, 자기부담_현금
                             FROM tenant.f_profile WHERE org_id=%s LIMIT 1""",
                        (org_id,)).fetchone()
    except Exception:
        return None                     # 모른다
    if not r:
        return {}
    return {k: v for k, v in zip(("협약총액", "정부지원_현금", "자기부담_현금"), r)
            if v is not None}

# ════════════════════════════════════════════════════════════════════════════
# (5) 전제 해소 3갈래 — `Agent.md` §4
# ════════════════════════════════════════════════════════════════════════════
def 전제해소(cur, 전제목록: list[dict], *, org_id, 사업명, 비목,
          f값: dict | None = None, 기록: bool = True) -> dict:
    """a 즉시검증 / b 인라인요청 / c unmapped_premise.

    a 와 b 를 가르는 건 **F 축에 값이 있느냐**뿐이다. 없으면 화면 안에서 30초 받아
    대부분 LLM 재호출 없이 재계산한다 — 그래서 b 는 판정을 막지 않는다.
    c 는 F 축 설계의 피드백 루프다. 빈도 높은 unmapped 가 다음 F 필드 후보가 된다.
    """
    f값 = f값 or {}
    a, b, c = [], [], []
    for p in 전제목록:
        경로 = list(p.get("매핑") or [])
        if p.get("미매핑") or not 경로:
            c.append(p)
        elif all(f값.get(x) is not None for x in 경로):
            p = dict(p, 검증됨=True)
            a.append(p)
        else:
            p = dict(p, 필요입력=[x for x in 경로 if f값.get(x) is None])
            b.append(p)
    if c and 기록:
        _unmapped_적재(cur, c, 사업명=사업명, 비목=비목)
    return {"즉시검증": a, "인라인요청": b, "미매핑": c}


def _unmapped_적재(cur, 전제들: list[dict], *, 사업명, 비목) -> None:
    """`tenant.unmapped_premise` 누적. 🔴 `UNIQUE NULLS NOT DISTINCT` 가 있어야 돈다.

    D1-b 가 그 제약을 넣기 전에는 `발생횟수+1` 이 영영 안 걸려 결핍 루프가 죽는다.
    제약이 아직 없으면 ON CONFLICT 가 터지므로, 없으면 조용히 INSERT 만 한다.
    """
    있음 = cur.execute("""SELECT 1 FROM pg_constraint
                           WHERE conrelid='tenant.unmapped_premise'::regclass
                             AND contype='u'""").fetchone()
    for p in 전제들:
        인자 = ((p.get("사실") or "")[:500], json.dumps(p.get("매핑") or [],
                ensure_ascii=False), 사업명, 비목)
        if 있음:
            cur.execute("""INSERT INTO tenant.unmapped_premise
                             (premise_text, 근거조항, 사업명, 비목, 발생횟수, 최초, 최근)
                           VALUES (%s,%s,%s,%s,1,now(),now())
                           ON CONFLICT (premise_text, 사업명, 비목)
                           DO UPDATE SET 발생횟수 = tenant.unmapped_premise.발생횟수 + 1,
                                         최근 = now()""", 인자)
        else:
            cur.execute("""INSERT INTO tenant.unmapped_premise
                             (premise_text, 근거조항, 사업명, 비목, 발생횟수, 최초, 최근)
                           VALUES (%s,%s,%s,%s,1,now(),now())""", 인자)


# ════════════════════════════════════════════════════════════════════════════
# (7) 로깅
# ════════════════════════════════════════════════════════════════════════════
_decisions_컬럼: set[str] | None = None


def _컬럼(cur, 테이블: str) -> set[str]:
    s, n = 테이블.split(".")
    return {r[0] for r in cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema=%s AND table_name=%s", (s, n)).fetchall()}


def decisions_적재(cur, 행: dict) -> int | None:
    """전건 insert. D 가 아직 `강등코드`·`경로`·`실패단계` 를 안 넣었으면 그 셋만 뺀다.

    스키마를 기다리며 판정을 멈추지 않는다 — 없는 컬럼은 빼고 있는 것만 넣는다.
    """
    global _decisions_컬럼
    if _decisions_컬럼 is None:
        _decisions_컬럼 = _컬럼(cur, "tenant.decisions")
    쓸것 = {k: v for k, v in 행.items() if k in _decisions_컬럼}
    if not 쓸것:
        return None
    키 = list(쓸것)
    q = (f'INSERT INTO tenant.decisions ({",".join(chr(34)+k+chr(34) for k in 키)}) '
         f'VALUES ({",".join(["%s"] * len(키))}) RETURNING decision_id')
    return cur.execute(q, [쓸것[k] for k in 키]).fetchone()[0]


# ════════════════════════════════════════════════════════════════════════════
# 판정 본체
# ════════════════════════════════════════════════════════════════════════════
class 주입실패(Exception):
    """A10 fault injection. 진짜 장애와 같은 경로로 흐르는지 보려고 던진다."""


def _빈응답(판정: str, 요약: str, **추가) -> dict:
    """실패의 기본값. 🔴 여기서 '가능'·'조건부'가 나오는 경로는 없어야 한다."""
    assert 판정 in ("판단불가", "불가"), f"실패 경로에서 '{판정}' 이 나왔다"
    기본 = dict(판정=판정, 요약=요약, 해야할일=[], 인용목록=[], 전제목록=[],
                신뢰등급=None, 버전스탬프=None, 참조사슬=[], 강등사유=[], 미매핑전제=[])
    기본.update(추가)
    return 기본


def 판정(질문: str, *, 사업명: str | None = None, org_id=None, dry: bool = False,
       기관ID: str | None = None, top_k: int = 5, conn=None, 기록: bool = True,
       plan_id: int | None = None,
       격리근거: list[dict] | None = None, 주입: str | None = None,
       게이트임계: float | None = None, 온도: float = 0.0,
       변형: str = "V0", _비목고정: str | None = None) -> dict:
    """(1)~(7). 동결 인터페이스 — 시그니처를 협의 없이 바꾸지 않는다.

    `dry=True` : LLM 을 부르지 않는다. (1) 은 규칙 정규화, (4) 는 프롬프트 조립까지만.
                 **GPU 를 열기 전에 77문항이 끝까지 도는지** 보는 것이 목적이다.
    `plan_id`  : 지출계획에 딸린 판정이면 그 id. `tenant.decisions.plan_id` 에 그대로
                 들어간다 — 서버가 판정 직후 UPDATE 로 뒤에서 잇던 이음매를 없앤다.
                 없으면 NULL(단건 판정). 컬럼이 없는 스키마에서는 조용히 빠진다.
    `주입`     : A10 fault injection. 'db'|'timeout'|'schema'|'empty'|'cite'
    """
    t0 = time.time()
    지연: dict[str, int] = {}
    경로: list[str] = []
    코드: list[str] = []
    사유: list[str] = []
    게이트임계 = 게이트B_임계 if 게이트임계 is None else 게이트임계

    def 잰다(이름: str, t: float) -> None:
        지연[이름] = int((time.time() - t) * 1000)

    닫기 = conn is None
    try:
        if 주입 == "db":
            raise 주입실패("DB 연결 실패(주입)")
        # 🔴 autocommit. (4) LLM 호출이 30초를 넘길 수 있는데 그동안 읽기 트랜잭션을
        #    붙들고 있으면 다른 세션의 DDL·VACUUM 이 막힌다. 쓰기는 단문 INSERT 하나뿐이라
        #    문장 단위 원자성으로 충분하다.
        conn = conn or db.connect(connect_timeout=5, autocommit=True)
    except Exception as e:
        # `Agent.md` §8: DB 연결 실패 → 503. 판정을 추측으로 만들지 않는다.
        return _빈응답("판단불가", "데이터베이스에 연결할 수 없어 판정을 내리지 않았습니다.",
                     강등사유=[f"DB 연결 실패: {type(e).__name__}"],
                     강등코드=[], 경로="실패", 실패단계="DB", HTTP=503,
                     지연ms={"총": int((time.time() - t0) * 1000)})

    try:
        cur = conn.cursor()

        # ── (1) 정규화 — LLM 1회 ─────────────────────────────────────────
        t = time.time()
        try:
            if 주입 == "timeout":
                raise LLM실패("read timeout(주입)")
            정규 , 메타1 = 정규화(질문, dry=dry)
        except LLM실패 as e:
            잰다("정규화", t)
            return _마무리(conn, cur, _빈응답(
                "판단불가", "질문을 정규화하지 못했습니다. 품목과 금액을 나눠 다시 알려주세요.",
                강등사유=[f"(1) 정규화 실패: {e}"], 강등코드=[], 경로="실패",
                실패단계="정규화", 지연ms=지연), 기록=False, 닫기=닫기)
        잰다("정규화", t)
        경로.append("1정규화")
        품목, 용도 = 정규.get("품목") or 질문[:40], 정규.get("용도") or ""

        # ── (2)-a 비목 확정 ──────────────────────────────────────────────
        t = time.time()
        후보 = _비목확정(cur, 품목, 사업명)
        잰다("비목확정", t)
        if not 후보 and 정규.get("비목후보"):
            # B 가 못 잡으면 (1) 의 후보를 쓴다. 출처를 남겨 두 경로를 구분한다.
            #
            # 🔴 (1) 의 산출 모양이 두 가지다 — 둘 다 받는다 (2026-09-02).
            #    LLM 모드 스키마는 `{비목, 신뢰도}` 객체를 강제하지만,
            #    dry 의 `규칙_정규화()` 는 **문자열 리스트**를 돌려준다.
            #    dict 만 가정했더니 정답셋 93문항 중 23건이 여기서
            #    `TypeError: string indices must be integers` 로 죽었다.
            #    LLM 이 스키마를 어겨 문자열을 섞어 보내도 같은 자리가 터지므로,
            #    모양을 걸러 받는 것이 dry 대응이 아니라 방어다.
            #    문자열에는 신뢰도가 없다 — 0.0 으로 둔다. 없는 숫자를 지어내지 않는다
            #    (0.0 이면 통과 조건 C 의 최소신뢰 0.35 에 안 걸려 갈래가 안 열린다).
            후보 = []
            for c in 정규["비목후보"]:
                if isinstance(c, str):
                    이름, 신뢰 = c, 0.0
                elif isinstance(c, dict):
                    이름, 신뢰 = c.get("비목"), c.get("신뢰도", 0.0)
                else:
                    continue
                if 이름:
                    후보.append({"비목": 이름, "신뢰도": 신뢰, "출처": "슬롯1"})
        # 🔴 갈래 재귀는 «이 비목으로 보면 어떻게 되나» 를 묻는 것이다. 고정된 비목만 남긴다.
        #    안 남기면 재귀 안에서 같은 후보가 또 나와 통과 조건 C 가 다시 열린다 (2026-09-02).
        if _비목고정:
            후보 = [c for c in 후보 if c.get("비목") == _비목고정] or 후보[:1]
        비목 = 후보[0]["비목"] if 후보 else None

        # ── 통과 조건 C: 비목이 갈리는가 ────────────────────────────────────
        갈림 = (len(후보) >= 2
                and 후보[0].get("신뢰도", 0) >= 게이트C_최소신뢰
                and 후보[1].get("신뢰도", 0) >= 게이트C_최소신뢰
                and (후보[0].get("신뢰도", 0) - 후보[1].get("신뢰도", 0)) < 게이트C_격차)
        # 🔴 `_비목고정` 이 종료 조건이다. 이게 없으면 무한 재귀가 된다 — 실측으로 확인했다
        #    (gold_id=360 「시제품 제작에 금 도금」 이 dry 에서 자기 자신을 300겹 넘게 쌓았다).
        if 갈림 and not 격리근거 and not _비목고정:
            경로.append("C비목갈림")
            결과 = []
            for c in 후보[:2]:
                r = 판정(질문, 사업명=사업명, org_id=org_id, dry=dry, 기관ID=기관ID,
                        top_k=top_k, conn=conn, 기록=False, 주입=주입,
                        격리근거=격리근거, _비목고정=c["비목"],
                        게이트임계=게이트임계, 온도=온도, 변형=변형)
                r["비목"] = c["비목"]
                결과.append(r)
            응답 = dict(결과[0])
            응답.update(판정="선택필요", 요약="비목이 갈립니다. 두 경우의 판정을 나란히 보여드립니다.",
                       갈래=결과, 경로="+".join(경로), 게이트="C",
                       비목후보=후보[:2], 정규화=정규,
                       지연ms={**지연, "총": int((time.time() - t0) * 1000)})
            return _마무리(conn, cur, 응답, 기록=기록, 닫기=닫기, 질문=질문,
                        사업명=사업명, org_id=org_id, 기관ID=기관ID,
                        plan_id=plan_id)

        # ── (2)-b L3 룰 조회 (먼저) · (2)-c 게이팅 ───────────────────────
        t = time.time()
        l3룰 = _l3룰(cur, org_id, 비목)
        게이팅 = _게이팅(l3룰)
        잰다("l3게이팅", t)

        # ── (2)-d 효력 결정 · (2)-e 금액 비교 ────────────────────────────
        t = time.time()
        룰 = _effective(cur, 사업명, 비목, 기관ID) if 비목 else None
        잰다("effective_rule", t)

        # ── 게이트 A: 금지목록 적중 → 즉답 "불가". LLM 0회 ───────────────
        t = time.time()
        금지 = _금지적중(cur, 품목, 용도, 사업명, 비목)
        잰다("금지적중", t)
        if 금지:
            경로.append("A금지적중")
            응답 = _빈응답("불가", f"금지 항목에 해당합니다 — {금지.get('예시')}",
                        강등사유=[], 강등코드=[], 경로="+".join(경로))
            응답.update(게이트="A", 비목=비목, 정규화=정규, 금지근거=금지,
                       지연ms={**지연, "총": int((time.time() - t0) * 1000)},
                       모델={"호출수": 0 if dry else 1})
            return _마무리(conn, cur, 응답, 기록=기록, 닫기=닫기, 질문=질문,
                        사업명=사업명, org_id=org_id, 기관ID=기관ID,
                        plan_id=plan_id)

        # ── (3)-a L3 통째 로드 ∥ (3)-b~e 검색 ────────────────────────────
        # 독립이라 병렬이다. L3 를 먼저 보는 것이 지연을 늘리지 않는다 (`Agent.md` §1).
        t = time.time()
        if 주입 == "empty":
            검색결과 = {"top5": [], "폐포": [], "참조사슬": [], "게이트값": 0.0,
                     "dangling": [], "후보수": 0}
            l3본문 = []
        elif 격리근거 is not None:
            검색결과 = {"top5": [], "폐포": [], "참조사슬": [], "게이트값": 1.0,
                     "dangling": [], "후보수": len(격리근거), "_출처": "격리(D6)"}
            l3본문 = []
        else:
            with ThreadPoolExecutor(max_workers=2) as ex:
                fl3 = ex.submit(_병렬_l3, org_id, 사업명)
                fse = ex.submit(_병렬_검색, 질문, 사업명, top_k)
                l3본문, l3err = fl3.result()
                검색결과, se_err = fse.result()
            if se_err:
                # 검색이 터지면 판정하지 않는다. 근거 없이 답을 만드는 게 최악이다.
                return _마무리(conn, cur, _빈응답(
                    "판단불가", "규정 검색에 실패해 판정을 내리지 않았습니다.",
                    강등사유=[f"(3) 검색 실패: {se_err}"], 강등코드=[], 경로="실패",
                    실패단계="검색", 지연ms=지연), 기록=False, 닫기=닫기)
        잰다("검색", t)
        경로.append("3검색")

        # ── 게이트 B: 스코어 미달 → 판단불가. LLM 1회에서 끝난다 ─────────
        # 예외: L3 게이팅 (3) — L3 에 인용할 명시 근거가 이미 있다 (`Agent.md` §3-2)
        l3단독가능 = (not 게이팅.get("need_upper")) and bool(l3본문)
        if (not 검색결과["top5"] or 검색결과["게이트값"] < 게이트임계) and not l3단독가능:
            경로.append("B스코어미달")
            응답 = _빈응답("판단불가",
                        "이 질문에 해당하는 규정을 찾지 못했습니다. 담당자 확인이 필요합니다.",
                        강등사유=[f"검색 게이트값 {검색결과['게이트값']:.3f} "
                                f"< 임계 {게이트임계} · top5 {len(검색결과['top5'])}건"],
                        강등코드=[], 경로="+".join(경로))
            응답.update(게이트="B", 비목=비목, 정규화=정규, 검색=검색결과,
                       참조사슬=검색결과["참조사슬"],
                       유사사례=_사례(cur, 질문, "판단불가"),
                       지연ms={**지연, "총": int((time.time() - t0) * 1000)},
                       모델={"호출수": 0 if dry else 1})
            return _마무리(conn, cur, 응답, 기록=기록, 닫기=닫기, 질문=질문,
                        사업명=사업명, org_id=org_id, 기관ID=기관ID,
                        plan_id=plan_id)

        # ── (4) 판정 조립 ────────────────────────────────────────────────
        t = time.time()
        프롬프트, s맵, 사슬 = 조립(cur, 질문, 정규, l3=l3본문 or None,
                        검색=검색결과["top5"] or None,
                        폐포=검색결과["폐포"] or None,
                        룰결과=b4_문장(룰), f요약=b5_문장(cur, org_id),
                        참조사슬=검색결과["참조사슬"], 변형=변형, 격리근거=격리근거)
        잰다("조립", t)
        경로.append("4조립")

        if not s맵:
            # 근거가 한 줄도 없으면 LLM 을 부르지 않는다. 부르면 지어낸다.
            return _마무리(conn, cur, _빈응답(
                "판단불가", "인용할 규정 원문이 없어 판정을 내리지 않았습니다.",
                강등사유=["s맵 0건 — B1·B2·B3 이 모두 비었다"], 강등코드=["NO_CITATION"],
                경로="+".join(경로), 실패단계="조립", 지연ms=지연), 기록=False, 닫기=닫기)

        if dry and 주입 not in ("schema", "cite"):
            # 드라이런은 여기까지다. LLM 을 부르지 않는다.
            응답 = _빈응답("판단불가", "[dry] LLM 을 부르지 않았다. 프롬프트 조립까지만.",
                        강등사유=[], 강등코드=[], 경로="+".join(경로 + ["dry중단"]))
            응답.update(게이트="D", 비목=비목, 정규화=정규, 검색=검색결과,
                       참조사슬=검색결과["참조사슬"], dry=True,
                       프롬프트길이=len(프롬프트), s맵크기=len(s맵),
                       s맵={k: list(v) for k, v in s맵.items()},
                       b4=bool(룰), b1=len(l3본문),
                       지연ms={**지연, "총": int((time.time() - t0) * 1000)},
                       모델={"호출수": 0})
            return _마무리(conn, cur, 응답, 기록=False, 닫기=닫기)

        t = time.time()
        try:
            if 주입 == "timeout":
                raise LLM실패("read timeout(주입)")
            # 🔴 사업명을 넘긴다. 안 넘기면 52개 code 가 통째로 enum 에 들어가
            #    이 사업과 무관한 해야할일(예: `기장대행한도_재도전`)이 제안된다.
            코드들 = 체크코드_enum(사업명=사업명) or None
            스키마 = 판정_스키마(s번호들=list(s맵), 코드들=코드들)
            # 🔴 주입은 **제 단계까지 가야** 검증이 된다. vLLM 없이 (4) 를 지나려면
            #    LLM 출력을 합성하는 수밖에 없다. 둘 다 (6) 이 잡아야 하는 것들이다:
            #      schema — 스키마 밖 필드({결과,이유}) → INVALID_JUDGMENT
            #      cite   — s맵 밖 S번호(S99)        → CITE_NOT_IN_MAP + NO_CITATION
            #    합성값이 "가능" 인 게 요점이다. 검증기가 이걸 판단불가로 못 내리면
            #    실패 경로에서 «틀린 가능» 이 새 나간다.
            if 주입 == "schema":
                출력, 메타4 = {"결과": "가능", "이유": "주입"}, {"지연ms": 0, "모델": "합성(주입)"}
            elif 주입 == "cite":
                출력, 메타4 = ({"판정": "가능", "요약": "주입", "해야할일": [],
                              "인용": ["S99"], "전제": []},
                             {"지연ms": 0, "모델": "합성(주입)"})
            else:
                출력, 메타4 = llm_호출(프롬프트, 스키마, 온도=온도, 최대토큰=1500)
        except LLM실패 as e:
            잰다("판정LLM", t)
            return _마무리(conn, cur, _빈응답(
                "판단불가", "판정 모델 호출에 실패해 결론을 내리지 않았습니다.",
                강등사유=[f"(4) LLM 실패: {e}"], 강등코드=[], 경로="+".join(경로),
                실패단계="판정LLM", 지연ms=지연), 기록=False, 닫기=닫기)
        잰다("판정LLM", t)

        # ── (6) 검증·강등 ────────────────────────────────────────────────
        t = time.time()
        if 주입 == "cite":
            출력 = dict(출력, 인용=["S99"], 전제=[])       # s맵 밖 S번호
        응답, 사유 = 검증(출력, s맵,
                      룰들=(룰 or {}).get("룰들"),
                      체크코드=코드들,
                      현재기관=기관ID, 사업명=사업명,
                      dangling=검색결과["dangling"],
                      l3게이팅=게이팅, 룰=룰,
                      # 층 B — 해야할일 설명 환각 대조. 추가 조회·추가 LLM 호출 0.
                      f사실=b5_값(cur, org_id), 프롬프트=프롬프트,
                      dsn=DSN)
        코드 = 응답.get("강등코드") or []
        잰다("검증", t)
        경로.append("6검증")

        # ── (5) 전제 해소 ────────────────────────────────────────────────
        t = time.time()
        해소 = 전제해소(cur, 응답.get("전제목록") or [], org_id=org_id,
                     사업명=사업명, 비목=비목, 기록=기록)
        잰다("전제해소", t)
        if 해소["미매핑"] and "PREMISE_UNMAPPED" not in 코드:
            코드.append("PREMISE_UNMAPPED")

        응답.update(게이트="D", 경로="+".join(경로), 비목=비목, 정규화=정규,
                   검색=검색결과, 참조사슬=사슬,                     # 🔴 A4: [] 하드코딩 제거
                   전제해소=해소, 강등코드=코드, 강등사유=사유,
                   s맵={k: list(v) for k, v in s맵.items()},
                   유사사례=_사례(cur, 질문, 응답.get("판정")),
                   지연ms={**지연, "총": int((time.time() - t0) * 1000)},
                   변형=변형,
                   모델={"호출수": 2, "변형": 변형, "정규화": 메타1.get("모델"),
                        "판정": 메타4.get("모델"), "판정지연ms": 메타4.get("지연ms")})
        return _마무리(conn, cur, 응답, 기록=기록, 닫기=닫기, 질문=질문,
                    사업명=사업명, org_id=org_id, 기관ID=기관ID,
                    plan_id=plan_id)

    except 주입실패 as e:
        return _마무리(conn, None, _빈응답(
            "판단불가", "장애가 발생해 판정을 내리지 않았습니다.",
            강등사유=[str(e)], 강등코드=[], 경로="실패", 실패단계="주입",
            지연ms=지연), 기록=False, 닫기=닫기)
    except Exception as e:
        # 🔴 예상 못 한 예외도 판단불가로 닫는다. 스택은 남기되 사용자에겐 안 준다.
        return _마무리(conn, None, _빈응답(
            "판단불가", "내부 오류로 판정을 내리지 않았습니다.",
            강등사유=[f"{type(e).__name__}: {str(e)[:200]}"], 강등코드=[],
            경로="실패", 실패단계="예외", 트레이스=traceback.format_exc()[-1200:],
            지연ms=지연), 기록=False, 닫기=닫기)


def _병렬_l3(org_id, 사업명):
    """스레드 안에서 자기 커넥션을 연다 — psycopg 커넥션은 공유하지 않는다."""
    if not org_id:
        return [], None
    try:
        # 🔴 autocommit. 읽기 트랜잭션을 붙들면 다른 세션의 DDL 과 교착난다
        #    (2026-08-31 8세션 병렬 중 DeadlockDetected 실측 — C 세션 보고).
        with db.connect(connect_timeout=5, autocommit=True) as c:
            return _l3로드(c.cursor(), org_id, 사업명), None
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


def _병렬_검색(질문, 사업명, top_k):
    try:
        with db.connect(connect_timeout=5, autocommit=True) as c:
            return _검색(c.cursor(), 질문, 사업명, top_k=top_k), None
    except Exception as e:
        return {"top5": [], "폐포": [], "참조사슬": [], "게이트값": 0.0,
                "dangling": [], "후보수": 0}, f"{type(e).__name__}: {e}"


def _사례(cur, 질문, 판정값):
    """🔴 판정이 판단불가로 **확정된 뒤에만** 부른다 (`RAG.md` §2-2 · F5 함수 레벨 강제)."""
    if 판정값 != "판단불가" or not _F_사례:
        return []
    try:
        return _F_사례(cur, 질문, k=3, 판정=판정값)
    except Exception:
        return []


def _마무리(conn, cur, 응답: dict, *, 기록: bool, 닫기: bool,
          질문: str = "", 사업명=None, org_id=None, 기관ID=None,
          plan_id=None) -> dict:
    """(7) decisions insert + 커밋 + 정리. 기록 실패가 판정을 죽이지 않는다."""
    try:
        if 기록 and cur is not None:
            행 = dict(
                org_id=org_id, 사업명=사업명, 기관id=기관ID, 질문원문=질문,
                plan_id=plan_id,
                정규화=json.dumps(응답.get("정규화") or {}, ensure_ascii=False),
                비목=응답.get("비목"),
                금액=(응답.get("정규화") or {}).get("금액"),
                판정=응답.get("판정"), 신뢰등급=응답.get("신뢰등급"),
                요약=응답.get("요약"), 버전스탬프=응답.get("버전스탬프"),
                인용=json.dumps(응답.get("인용목록") or [], ensure_ascii=False, default=str),
                해야할일=json.dumps(응답.get("해야할일") or [], ensure_ascii=False),
                전제=json.dumps(응답.get("전제목록") or [], ensure_ascii=False, default=str),
                참조사슬=json.dumps(응답.get("참조사슬") or [], ensure_ascii=False, default=str),
                미매핑전제=json.dumps(응답.get("미매핑전제") or [], ensure_ascii=False),
                강등사유=응답.get("강등사유") or [],
                강등코드=응답.get("강등코드") or [],
                경로=응답.get("경로"), 실패단계=응답.get("실패단계"),
                지연ms=json.dumps(응답.get("지연ms") or {}, ensure_ascii=False),
                모델=json.dumps(응답.get("모델") or {}, ensure_ascii=False),
                검색스냅샷=json.dumps({"s맵": 응답.get("s맵") or {},
                                   "top5": (응답.get("검색") or {}).get("top5") or [],
                                   "게이트값": (응답.get("검색") or {}).get("게이트값"),
                                   "dangling": (응답.get("검색") or {}).get("dangling") or []},
                                  ensure_ascii=False, default=str),
                코퍼스버전=코퍼스버전(cur))
            응답["decision_id"] = decisions_적재(cur, 행)
        if cur is not None:
            conn.commit()
    except Exception as e:
        응답.setdefault("강등사유", []).append(f"(7) decisions 적재 실패: {type(e).__name__}: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        if 닫기 and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return 응답


_코퍼스버전: str | None = None


def 코퍼스버전(cur) -> str:
    """재현성의 축. 규정 모음이 바뀌면 같은 질문의 답이 바뀔 수 있다.

    🔴 함수·변수 이름의 «코퍼스» 는 그대로 둔다 — `eval.runs` 에 그 키로 들어간다.
    """
    global _코퍼스버전
    if _코퍼스버전 is None:
        d, c = cur.execute("SELECT (SELECT count(*) FROM corpus.documents WHERE status='active'), "
                           "(SELECT count(*) FROM corpus.chunks)").fetchone()
        _코퍼스버전 = f"docs{d}/chunks{c}"
    return _코퍼스버전


# ════════════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════════════
_주입종류 = ("db", "timeout", "schema", "empty", "cite")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--q")
    ap.add_argument("--사업명")
    ap.add_argument("--org-id")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--golden", action="store_true", help="정답셋 전량 (드라이런용)")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--fault", help="|".join(_주입종류) + "|all")
    ap.add_argument("--no-log", action="store_true", help="decisions 기록 생략")
    ap.add_argument("--out")
    ap.add_argument("--변형", default="V0",
                    help="A12 프롬프트 변형. V0=기준선 · V1~V6 (assemble_context.변형들)")
    ap.add_argument("--eval-log", action="store_true", dest="eval_log",
                    help="eval.runs 에 기록 (D 의 eval_store 경유)")
    a = ap.parse_args()

    print(f"모듈 상태: " + " ".join(f"{k}={'실물' if v else 'STUB'}"
                                for k, v in 모듈상태.items()), file=sys.stderr)

    if a.fault:
        종류 = list(_주입종류) if a.fault == "all" else [a.fault]
        나쁨 = 0
        for f in 종류:
            # 🔴 dry 로 돈다. vLLM 없이도 5경로가 **각자 제 단계에서** 걸려야 한다 —
            #    서버가 없어서 (1) 에서 다 죽으면 아무것도 검증한 게 아니다.
            r = 판정(a.q or "노트북 200만원 구매해도 되나요", 사업명=a.사업명,
                    dry=True, 기록=False, 주입=f)
            ok = r["판정"] == "판단불가"
            나쁨 += 0 if ok else 1
            print(f"{'✅' if ok else '🔴'} 주입={f:8} 판정={r['판정']:6} "
                  f"실패단계={r.get('실패단계')} 게이트={r.get('게이트')} "
                  f"코드={r.get('강등코드')} · {str(r.get('강등사유'))[:110]}")
        print("\n" + ("✅ 전 실패 경로가 판단불가로 닫힌다" if not 나쁨
                     else f"🔴 {나쁨}건이 판단불가가 아니다 — 배포 불가"))
        sys.exit(1 if 나쁨 else 0)

    if a.golden:
        워밍업()
        with db.connect(autocommit=True) as conn:
            # 🔴 D2 이후: 공통 27문항은 `사업명 IS NULL` + `적용범위` 에 원표기가 있다.
            #    사업명='공통...' 을 기대하는 코드는 그 자리에서 0건이 된다.
            컬럼 = {r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE "
                "table_schema='eval' AND table_name='golden_set'").fetchall()}
            적용 = "적용범위" if "적용범위" in 컬럼 else "NULL::text"
            rows = conn.execute(f"SELECT gold_id, 세트, 질문, 사업명, 정답판정, {적용} "
                                "FROM eval.golden_set ORDER BY gold_id").fetchall()
        if a.limit:
            rows = rows[:a.limit]
        out = []
        t0 = time.time()
        for gid, 세트, q, 사업, 정답, 적용범위 in rows:
            사업키 = None if (적용범위 or (사업 or "").startswith("공통")) else 사업
            r = 판정(q, 사업명=사업키, dry=a.dry, 기록=not a.no_log, 변형=a.변형)
            out.append({"gold_id": gid, "세트": 세트, "정답": 정답, **r})
            print(f"{gid:3} [{세트:4}] 게이트={r.get('게이트')} 경로={r.get('경로')} "
                  f"S={r.get('s맵크기', len(r.get('s맵') or {}))} "
                  f"프롬프트={r.get('프롬프트길이', 0):,}자 "
                  f"{r.get('지연ms', {}).get('총', 0):,}ms "
                  + (f"🔴{r.get('실패단계')}" if r.get("실패단계") else ""))
        print(f"\n{len(out)}건 · {time.time()-t0:.0f}초 · 변형={a.변형}")
        if a.eval_log:
            # 🔴 `설정` 에 **사업필터와 변형을 반드시 박는다.** 이게 없으면 내일 이 숫자가
            #    어느 조건에서 나온 건지 못 가린다 — 그게 오늘 밤의 유일한 산출물인데.
            try:
                from eval_store import 기록 as _기록
                n = len(out) or 1
                일치 = sum(1 for r in out if r.get("판정") == r.get("정답"))
                치명 = sum(1 for r in out if r.get("정답") in ("불가", "조건부")
                          and r.get("판정") == "가능")
                불가 = sum(1 for r in out if r.get("판정") == "판단불가")
                rid = _기록({"종류": "e2e",
                           "설정": {"변형": a.변형, "사업필터": 사업필터,
                                  "게이트B임계": 게이트B_임계, "dry": a.dry,
                                  "top_k": 5, "온도": 0.0},
                           "문항수": len(out),
                           "지표": {"일치율": 일치 / n * 100, "치명오답률": 치명 / n * 100,
                                  "판단불가율": 불가 / n * 100,
                                  "일치": 일치, "치명": 치명, "판단불가": 불가},
                           "라벨": f"A12-{a.변형}"},
                          [{"gold_id": r["gold_id"], "예측": r.get("판정"),
                            "정답": r.get("정답"),
                            "적중": r.get("판정") == r.get("정답"),
                            "원출력": {k: r.get(k) for k in
                                     ("판정", "인용목록", "전제목록", "강등코드",
                                      "강등사유", "게이트", "경로", "지연ms", "s맵")}}
                           for r in out])
                print(f"eval.runs run_id={rid}")
            except Exception as e:
                print(f"⚠️ eval.runs 기록 실패(결과 파일은 남았다): "
                      f"{type(e).__name__}: {e}")
        if a.out:
            from pathlib import Path
            Path(a.out).parent.mkdir(parents=True, exist_ok=True)
            with open(a.out, "w", encoding="utf-8") as f:
                for r in out:
                    f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
            print(f"-> {a.out}")
        return

    if not a.q:
        ap.error("--q · --golden · --fault 중 하나")
    r = 판정(a.q, 사업명=a.사업명, org_id=a.org_id, dry=a.dry, 기록=not a.no_log)
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
