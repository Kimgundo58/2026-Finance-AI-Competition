# -*- coding: utf-8 -*-
"""판정 오케스트레이터 — (1) 정규화 → (2) 룰 조회 → (3) 검색 → (4) 판정 조립 → (5) 전제 해소
→ (6) 검증 → (7) 기록. LLM 호출은 (1)·(4) 두 번이고 나머지는 코드다.

게이트: A 금지목록 적중(즉답 불가, LLM 0회) · B 검색 결과 0건(판단불가, LLM 1회)
· C 비목 후보가 갈림(1순위로 판정하고 후보를 응답에 싣는다) · D 정상.
모든 실패(DB·타임아웃·스키마·검색 0건·인용 검증 실패)는 판단불가로 닫는다.

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

# 임계치
# 게이트 B: dense 코사인 최고값이 이 값 미만이면 판단불가. hit/miss 분포가 겹쳐 분리 임계가
# 없으므로 0.0 이 기본이고, 실질적으로 top5 가 0건일 때만 발화한다.
게이트B_임계 = float(os.environ.get("SUDDOE_GATE_B", "0.0"))

# 게이트 C: 비목 후보 1·2위 신뢰도 차이가 이 값 미만이면 갈렸다고 본다.
게이트C_격차 = float(os.environ.get("SUDDOE_GATE_C", "0.15"))
게이트C_최소신뢰 = 0.35
# (4) 판정 호출의 max_tokens. 기록되는 값과 실제 쓰이는 값이 같은 이름을 보도록 한 곳에 둔다.
판정_최대토큰 = int((os.environ.get("SUDDOE_JUDGE_MAX_TOKENS") or os.environ.get("SUDDOE_판정_최대토큰", "1500")))

# 강등코드 18종 (검증 단계가 발행한다)
강등코드_전체: tuple[str, ...] = (
    "INVALID_JUDGMENT", "CITE_NOT_IN_MAP", "CITE_DB_MISSING", "CITE_HANG_MISMATCH",
    "PREMISE_NO_BASIS", "PREMISE_BASIS_NOT_IN_MAP", "PREMISE_ENUM", "PREMISE_UNMAPPED",
    "NO_CITATION", "VLM_DOWNGRADE", "B_GRADE_DOWNGRADE", "UNVERIFIED_RULE",
    "TASK_CODE_INVALID", "L3_ONLY_DOWNGRADE", "TENANT_LEAK", "DANGLING_WARN",
    "DOMAIN_WARN", "PRECEDENCE_FLIP",
)


# 선택 모듈 — 있으면 쓰고 없으면 스텁
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

모듈상태 = {"C": bool(_C_검색), "B": bool(_B_effective), "E": bool(_E_로드)}


def 워밍업() -> None:
    """프로세스당 1회. 임베딩 모델·kiwi 로드를 첫 판정 밖으로 뺀다."""
    try:
        import retrieve
        retrieve.워밍업()
    except Exception as e:
        print(f"⚠️ 검색 워밍업 실패(계속 진행): {type(e).__name__}: {e}", file=sys.stderr)


# 검색 스텁: eval_retrieval.py 의 SQL 로 실제 검색한다. 폐포·참조사슬·dangling 은 빈 값이다.
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
    # 게이트값 = dense 코사인 최고값. pgvector `<=>` 는 코사인 거리라 `1 - 거리` 다
    게이트값 = 0.0
    if dense:
        cur.execute("SELECT 1 - (embedding <=> %s::extensions.vector(1024)) "
                    "FROM corpus.chunks WHERE chunk_id=%s", (vec, dense[0]))
        게이트값 = float(cur.fetchone()[0])
    return {"top5": rrf([dense, bm])[:top_k], "폐포": [], "참조사슬": [],
            "게이트값": 게이트값, "dangling": [], "후보수": len(set(dense) | set(bm)),
            "_출처": "STUB:A(eval_retrieval SQL)"}


# 판정 경로는 사업 필터를 켠다 — 끄면 다른 사업의 규정이 인용 근거(B2)에 실린다.
# `retrieve.사업필터_기본` 은 False 로 둔 채 호출부에서만 켠다 (검색 평가 기준선 보존).
사업필터 = (os.environ.get("SUDDOE_PROGRAM_FILTER") or os.environ.get("SUDDOE_사업필터", "1")) != "0"


def _검색(cur, 질문, 사업명, *, top_k=5) -> dict:
    try:
        r = (_C_검색 or _스텁_검색)(cur, 질문, 사업명, top_k=top_k, 사업필터=사업필터)
    except TypeError:
        r = (_C_검색 or _스텁_검색)(cur, 질문, 사업명, top_k=top_k)   # 스텁 호환
    # 0건이어도 None 이 아니라 빈 리스트로 맞춘다
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


def _effective(cur, 사업명, 비목, 기관ID=None, 수치=None):
    # `수치=` 를 넘겨야 (2)-e 금액 비교가 돈다
    return _B_effective(cur, 사업명, 비목, 기관ID, 수치=수치) if _B_effective else None  # STUB: B


def _게이팅(l3룰) -> dict:
    if _B_게이팅:
        return _B_게이팅(l3룰)
    # STUB: B — L3 가 "가능" 이면 상위 규범 확인(need_upper)을 강제한다
    if not l3룰:
        return {"need_upper": True, "seed_refs": []}
    if l3룰.get("참조만"):
        return {"need_upper": True, "seed_refs": list(l3룰.get("근거") or [])}
    if l3룰.get("허용") in ("불가", "조건부"):
        return {"need_upper": False, "seed_refs": []}
    return {"need_upper": True, "seed_refs": []}          # 허용='가능' → 상위 확인 강제


def _l3로드(cur, org_id, 사업명, 비목=None) -> list[dict]:
    # `비목` 은 정렬 힌트다 (SUDDOE_L3_비목순=on 일 때만 발효). 조를 버리지는 않는다.
    if _E_로드 and org_id:
        try:
            return _E_로드(cur, org_id, 사업명, 비목) or []
        except TypeError:            # 3인자 서명이면 그대로 부른다
            return _E_로드(cur, org_id, 사업명) or []
    return []                                                    # STUB: E


def _l3룰(cur, org_id, 비목):
    return _E_l3룰(cur, org_id, 비목) if (_E_l3룰 and org_id) else None          # STUB: E


# (2)-e·B4 — 룰 결과를 문장으로. 원시 한도값은 프롬프트에 넣지 않는다
def b4_문장(룰: dict | None) -> str | None:
    """B4 블록 본문. 룰의 `B4문장` 이 있으면 그대로, 없으면 비교가 끝난 문장을 최소한으로 만든다."""
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
    """B5 블록 본문 — 테넌트 F 프로필(협약 기간·현금 재원) 요약. 현물은 없다."""
    if not org_id:
        return None
    try:
        r = cur.execute("""SELECT 협약시작일, 협약종료일, 정부지원_현금, 자기부담_현금
                             FROM tenant.f_profile WHERE org_id=%s LIMIT 1""",
                        (org_id,)).fetchone()
    except Exception as e:
        # 실패한 문장이 트랜잭션을 abort 시켜 뒤 쿼리가 전부 죽으므로 rollback 으로 되살린다
        print(f"🔴 b5_문장 조회 실패 — {type(e).__name__}: {e}", file=sys.stderr)
        try:
            cur.connection.rollback()
        except Exception:
            pass
        return None
    if not r:
        return None
    이름 = ("협약시작일", "협약종료일", "정부지원(현금)", "자기부담(현금)")
    return "\n".join(f"{n}: {v if v is not None else '미입력'}" for n, v in zip(이름, r))

def b5_값(cur, org_id) -> dict | None:
    """B5 의 값 (검증 대조용 원본). F1·F4·F3 을 각각 독립 조회한다.

    반환: None = 조회를 전부 못 했다(모른다) · {} = F축이 없다(게스트) · {...} = 실제 값.
    """
    if not org_id:
        return {}                       # 게스트. '모른다' 가 아니라 '없다' 다

    값: dict = {}
    성공 = False

    # F1 (f_profile)
    try:
        r = cur.execute("""SELECT 협약시작일, 협약종료일, 정부지원_현금, 자기부담_현금,
                                  과업범위요약
                             FROM tenant.f_profile WHERE org_id=%s LIMIT 1""",
                        (org_id,)).fetchone()
        성공 = True
        if r:
            값.update({k: v for k, v in zip(("협약시작일", "협약종료일", "정부지원_현금",
                                            "자기부담_현금", "과업범위요약"), r)
                       if v is not None})
    except Exception as e:
        print(f"🔴 b5_값 F1(f_profile) 조회 실패 — {type(e).__name__}: {e}", file=sys.stderr)
        try:
            cur.connection.rollback()   # abort 된 트랜잭션을 되살려 뒤 조회를 살린다
        except Exception:
            pass

    # F4 (f_personnel) — org_id 가 없어 f_profile.profile_id 로 조인한다.
    # 직원별 여러 행일 수 있으므로 1행일 때만 스칼라로 접고, 0행·2행 이상은 비운다(모르면 보류).
    try:
        cur.execute("""SELECT p.역할, p.고용형태, p.타사업참여율, p.소속기관유형, p.겸직
                         FROM tenant.f_personnel p
                         JOIN tenant.f_profile pr USING (profile_id)
                        WHERE pr.org_id=%s""", (org_id,))
        인원행 = cur.fetchall()
        성공 = True
        if len(인원행) == 1:
            값.update({k: v for k, v in zip(
                ("역할", "고용형태", "타사업참여율", "소속기관유형", "겸직"), 인원행[0])
                if v is not None})
        elif len(인원행) > 1:
            print(f"⚠️ b5_값: org={org_id} f_personnel {len(인원행)}행 — "
                  "한 값으로 못 접어 F4 를 비운다(모르면 보류)", file=sys.stderr)
    except Exception as e:
        print(f"🔴 b5_값 F4(f_personnel) 조회 실패 — {type(e).__name__}: {e}", file=sys.stderr)
        try:
            cur.connection.rollback()
        except Exception:
            pass

    # F3 (f_exec) — 집행 건별 로그. F4 와 같은 규칙(1행만 접는다)을 적용한다.
    try:
        cur.execute("""SELECT e.비목, e.재원, e.거래처, e.인력역할, e.귀속월, e.금액
                         FROM tenant.f_exec e
                         JOIN tenant.f_profile pr USING (profile_id)
                        WHERE pr.org_id=%s""", (org_id,))
        집행행 = cur.fetchall()
        성공 = True
        if len(집행행) == 1:
            값.update({k: v for k, v in zip(
                ("비목", "재원", "거래처", "인력역할", "귀속월", "금액"), 집행행[0])
                if v is not None})
        elif len(집행행) > 1:
            print(f"⚠️ b5_값: org={org_id} f_exec {len(집행행)}행 — "
                  "한 값으로 못 접어 F3 를 비운다(모르면 보류)", file=sys.stderr)
    except Exception as e:
        print(f"🔴 b5_값 F3(f_exec) 조회 실패 — {type(e).__name__}: {e}", file=sys.stderr)
        try:
            cur.connection.rollback()
        except Exception:
            pass

    if not 성공:
        return None                     # 셋 다 실패 — 모른다
    return 값


def 증빙_발급처(cur, 룰: dict | None) -> list[dict]:
    """`룰.증빙`(이름 배열)에 `corpus.evidence_sources.발급처` 를 조인한다. 화면 「결제 후」용.

    이름이 안 맞으면 발급처를 None 으로 두고 이름만 낸다.
    """
    이름들 = list((룰 or {}).get("증빙") or [])
    if not 이름들:
        return []
    cur.execute(
        'SELECT "증빙명", "발급처" FROM corpus.evidence_sources WHERE "증빙명" = ANY(%s)',
        (이름들,))
    발급처 = {r[0]: r[1] for r in cur.fetchall()}
    return [{"증빙명": n, "발급처": 발급처.get(n)} for n in 이름들]


# (5) 전제 해소 3갈래 — 즉시검증 / 인라인요청 / 미매핑
def f값_경로키(값: dict | None) -> dict:
    """`b5_값` 의 컬럼명 키를 전제.매핑의 경로 키(`F1.정부지원.현금` 꼴)로 바꾼다.

    None 은 {} 로 돌려준다.
    """
    if 값 is None:
        return {}
    # 축 번호는 테이블이 아니라 `llm_validate.py` 의 축 정의(F축_테이블)를 따른다 —
    # `과업범위요약` 은 f_profile 에 살지만 F2 다.
    특례 = {
        "과업범위요약": "F2",
        # F4 — tenant.f_personnel
        "역할": "F4", "고용형태": "F4", "타사업참여율": "F4",
        "소속기관유형": "F4", "겸직": "F4",
        # F3 — tenant.f_exec
        "비목": "F3", "재원": "F3", "거래처": "F3",
        "인력역할": "F3", "귀속월": "F3", "금액": "F3",
    }
    return {f'{특례.get(k, "F1")}.{k.replace("_", ".")}': v for k, v in 값.items()}


def 전제해소(cur, 전제목록: list[dict], *, org_id, 사업명, 비목,
          f값: dict | None = None, 기록: bool = True) -> dict:
    """전제를 a 즉시검증 / b 인라인요청 / c 미매핑 으로 가른다. a·b 는 F 축에 값이 있느냐로 갈린다."""
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
    """`tenant.unmapped_premise` 에 누적한다. UNIQUE 제약이 없으면 ON CONFLICT 없이 INSERT 만 한다."""
    # SAVEPOINT 로 되감는다 — abort 된 트랜잭션은 뒤따르는 decisions INSERT 까지 죽인다.
    # autocommit 이면 SAVEPOINT 를 쓸 수 없어 연결 모드를 보고 건다.
    _sp = False
    try:
        if not getattr(getattr(cur, "connection", None), "autocommit", True):
            cur.execute("SAVEPOINT _unmapped_적재")
            _sp = True
        있음 = cur.execute("""SELECT 1 FROM pg_constraint
                               WHERE conrelid='tenant.unmapped_premise'::regclass
                                 AND contype='u'""").fetchone()
        for p in 전제들:
            인자 = ((p.get("사실") or "")[:500], json.dumps(p.get("매핑") or [],
                    ensure_ascii=False), 사업명, 비목)
            _unmapped_한건(cur, 있음, 인자)
        if _sp:
            cur.execute("RELEASE SAVEPOINT _unmapped_적재")
    except Exception as e:
        if _sp:
            try:
                cur.execute("ROLLBACK TO SAVEPOINT _unmapped_적재")
            except Exception:
                pass
        # 통계 적재 실패가 판정을 죽이면 안 된다 — 로그에만 남긴다 (강등사유에 넣지 않는다)
        sys.stderr.write("[unmapped 적재 실패 · 판정은 계속한다] "
                         + type(e).__name__ + ": " + str(e) + chr(10))
        return


def _unmapped_한건(cur, 있음, 인자) -> None:
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


# (7) 로깅
_decisions_컬럼: set[str] | None = None


def _컬럼(cur, 테이블: str) -> set[str]:
    s, n = 테이블.split(".")
    return {r[0] for r in cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema=%s AND table_name=%s", (s, n)).fetchall()}


def decisions_적재(cur, 행: dict) -> int | None:
    """`tenant.decisions` 에 한 건 넣는다. 테이블에 없는 컬럼은 빼고 있는 것만 넣는다."""
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


# 판정 본체
class 주입실패(Exception):
    """fault injection 용 예외. 진짜 장애와 같은 경로로 흐르는지 본다."""


def _빈응답(판정: str, 요약: str, **추가) -> dict:
    """실패 응답의 기본값. '가능'·'조건부' 는 여기서 나오지 않는다."""
    assert 판정 in ("판단불가", "불가"), f"실패 경로에서 '{판정}' 이 나왔다"
    기본 = dict(판정=판정, 요약=요약, 해야할일=[], 인용목록=[], 전제목록=[],
                신뢰등급=None, 버전스탬프=None, 참조사슬=[], 강등사유=[], 미매핑전제=[])
    기본.update(추가)
    # 실패경로: 판단불가가 모델의 판단인지 실패인지 집계에서 가르는 플래그
    기본["실패경로"] = "실패단계" in 추가
    return 기본


def 판정(질문: str, *, 사업명: str | None = None, org_id=None, dry: bool = False,
       기관ID: str | None = None, top_k: int = 5, conn=None, 기록: bool = True,
       plan_id: int | None = None,
       격리근거: list[dict] | None = None, 주입: str | None = None,
       게이트임계: float | None = None, 온도: float = 0.0,
       변형: str = "V7", _비목고정: str | None = None,
       정규화결과: dict | None = None,
       폐포사용: bool = True,
       사용자F값: dict | None = None) -> dict:
    """질문 하나를 (1)~(7) 로 판정한다.

    `dry`       : LLM 을 부르지 않는다. (1) 은 규칙 정규화, (4) 는 프롬프트 조립까지만.
    `plan_id`   : 지출계획에 딸린 판정이면 그 id (`tenant.decisions.plan_id`). 없으면 NULL.
    `주입`      : fault injection. 'db'|'timeout'|'schema'|'empty'|'cite'
    `변형`      : 프롬프트 변형 (assemble_context.변형들).
    `_비목고정` : 이 비목으로 고정해 판정한다 (사용자가 화면에서 비목을 고른 뒤 재판정용).
    `정규화결과`: 이미 돈 (1) 의 산출을 주면 (1) 을 건너뛴다. 이름을 `정규화` 로 두면
                  모듈 전역 함수를 가리므로 다른 이름이다.
    `폐포사용`  : False 면 (4) 조립에 B3(참조 확장·폐포)을 넣지 않는다. 검색 결과는 그대로다.
    `사용자F값` : 화면에서 방금 받은 심층질문 답. `b5_값()` 과 같은 컬럼명 모양이며
                  DB 저장값보다 우선한다.
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
        # autocommit — LLM 호출 동안 읽기 트랜잭션을 붙들면 다른 세션의 DDL·VACUUM 이 막힌다
        conn = conn or db.connect(connect_timeout=5, autocommit=True)
        # 새로 연 커넥션은 미들웨어의 GUC 를 물려받지 못한다. `app.org_id` 를 세우지 않으면
        # (7) INSERT 가 RLS 에 막혀 decision_id 가 NULL 이 된다. autocommit 이라 세션 레벨로 세운다.
        # 받은 conn(닫기=False)은 호출자가 이미 세웠다고 보고 건드리지 않는다.
        if 닫기 and org_id:
            conn.execute("SELECT set_config('app.org_id', %s, false)", (str(org_id),))
    except Exception as e:
        # DB 연결 실패 → 503 판단불가
        return _빈응답("판단불가", "데이터베이스에 연결할 수 없어 판정을 내리지 않았습니다.",
                     강등사유=[f"DB 연결 실패: {type(e).__name__}"],
                     강등코드=[], 경로="실패", 실패단계="DB", HTTP=503,
                     지연ms={"총": int((time.time() - t0) * 1000)})

    try:
        cur = conn.cursor()

        # (1) 정규화 — LLM 1회. 이미 돈 결과를 받으면 건너뛴다
        t = time.time()
        # dict 인지만 본다. 모양 방어는 아래 (2)-a 한 곳에서 한다
        외부정규화 = isinstance(정규화결과, dict) and bool(정규화결과)
        if 외부정규화:
            정규, 메타1 = 정규화결과, {"모델": "생략(호출부 제공)", "호출수": 0}
            잰다("정규화", t)
            경로.append("1정규화(외부)")
        else:
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
        # 실제로 몇 번 불렀는가
        정규화호출 = 0 if (dry or 외부정규화) else 1
        품목, 용도 = 정규.get("품목") or 질문[:40], 정규.get("용도") or ""

        # (2)-a 비목 확정
        t = time.time()
        후보 = _비목확정(cur, 품목, 사업명)
        잰다("비목확정", t)
        if not 후보 and 정규.get("비목후보"):
            # 룰 조회가 못 잡으면 (1) 의 후보를 쓴다. 출처를 남겨 두 경로를 구분한다.
            # (1) 의 산출은 `{비목, 신뢰도}` dict 또는 문자열(dry)이라 둘 다 받는다.
            # 문자열에는 신뢰도가 없어 0.0 으로 둔다.
            후보 = []
            _후보원본 = 정규["비목후보"]
            if isinstance(_후보원본, (str, dict)):
                _후보원본 = [_후보원본]          # 단건을 그대로 준 경우
            elif not isinstance(_후보원본, (list, tuple)):
                _후보원본 = []                   # dict 를 순회하면 키가 비목이 된다 — 막는다
            for c in _후보원본:
                if isinstance(c, str):
                    이름, 신뢰 = c, 0.0
                elif isinstance(c, dict):
                    이름, 신뢰 = c.get("비목"), c.get("신뢰도", 0.0)
                else:
                    continue
                if 이름:
                    후보.append({"비목": 이름, "신뢰도": 신뢰, "출처": "슬롯1"})
        # 비목 고정이면 그 후보만 남긴다. 후보에 없으면 다른 비목으로 물러나지 않고 새로 세운다
        if _비목고정:
            후보 = ([c for c in 후보 if c.get("비목") == _비목고정]
                  or [{"비목": _비목고정, "신뢰도": 0.0, "출처": "갈래고정"}])
        비목 = 후보[0]["비목"] if 후보 else None

        # 게이트 C: 비목이 갈리는가
        갈렸다 = False
        갈림 = (len(후보) >= 2
                and 후보[0].get("신뢰도", 0) >= 게이트C_최소신뢰
                and 후보[1].get("신뢰도", 0) >= 게이트C_최소신뢰
                and (후보[0].get("신뢰도", 0) - 후보[1].get("신뢰도", 0)) < 게이트C_격차)
        # 갈려도 1순위로 한 번만 판정하고, 갈렸다는 사실과 후보를 응답에 싣는다.
        # 최종 비목은 사용자가 화면에서 확정하고 `_비목고정` 으로 재판정한다.
        if 갈림 and not 격리근거 and not _비목고정:
            경로.append("C비목갈림")
            갈렸다 = True
        # 갈림필드는 모든 종료 경로(실패 응답 포함)에 같이 싣는다
        갈림필드 = ({"게이트": "C", "비목갈림": True, "비목후보": 후보[:2]}
                  if 갈렸다 else {})

        # (2)-b L3 룰 조회 · (2)-c 게이팅
        t = time.time()
        l3룰 = _l3룰(cur, org_id, 비목)
        게이팅 = _게이팅(l3룰)
        잰다("l3게이팅", t)

        # (2)-d 효력 결정 · (2)-e 금액 비교
        t = time.time()
        # None 인 키는 넣지 않는다 — 「넘겼는데 비교가 안 됐다」와 「넘길 게 없었다」를 가른다
        수치 = {k: v for k, v in (("금액", 정규.get("금액")),) if v is not None}
        룰 = _effective(cur, 사업명, 비목, 기관ID, 수치=수치 or None) if 비목 else None
        잰다("effective_rule", t)

        # 게이트 A: 금지목록 적중 → 즉답 "불가". LLM 0회
        t = time.time()
        금지 = _금지적중(cur, 품목, 용도, 사업명, 비목)
        잰다("금지적중", t)
        if 금지:
            경로.append("A금지적중")
            응답 = _빈응답("불가", f"금지 항목에 해당합니다 — {금지.get('예시')}",
                        강등사유=[], 강등코드=[], 경로="+".join(경로))
            응답.update(게이트="A", 비목=비목, 정규화=정규, 금지근거=금지,
                       지연ms={**지연, "총": int((time.time() - t0) * 1000)},
                       모델={"호출수": 정규화호출})
            return _마무리(conn, cur, dict(응답, **갈림필드), 기록=기록, 닫기=닫기, 질문=질문,
                        사업명=사업명, org_id=org_id, 기관ID=기관ID,
                        plan_id=plan_id)

        # (3)-a L3 통째 로드 ∥ (3)-b~e 검색 — 독립이라 병렬이다
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
                fl3 = ex.submit(_병렬_l3, org_id, 사업명, 비목)
                fse = ex.submit(_병렬_검색, 질문, 사업명, top_k)
                l3본문, l3err = fl3.result()
                검색결과, se_err = fse.result()
            if se_err:
                # 검색이 터지면 판정하지 않는다
                return _마무리(conn, cur, _빈응답(
                    "판단불가", "규정 검색에 실패해 판정을 내리지 않았습니다.",
                    강등사유=[f"(3) 검색 실패: {se_err}"], 강등코드=[], 경로="실패",
                    실패단계="검색", 지연ms=지연, **갈림필드), 기록=False, 닫기=닫기)
        잰다("검색", t)
        경로.append("3검색")

        # 게이트 B: 스코어 미달 → 판단불가. LLM 1회에서 끝난다.
        # 예외: L3 에 인용할 명시 근거가 이미 있으면 통과한다
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
                       지연ms={**지연, "총": int((time.time() - t0) * 1000)},
                       모델={"호출수": 정규화호출})
            return _마무리(conn, cur, dict(응답, **갈림필드), 기록=기록, 닫기=닫기, 질문=질문,
                        사업명=사업명, org_id=org_id, 기관ID=기관ID,
                        plan_id=plan_id)

        # (4) 판정 조립
        t = time.time()
        # check_items 후보는 조립 전에 구해 프롬프트에 싣는다. 사업명을 안 넘기면 전체가 들어간다
        코드들 = 체크코드_enum(사업명=사업명) or None
        프롬프트, s맵, 사슬 = 조립(cur, 질문, 정규, l3=l3본문 or None,
                        검색=검색결과["top5"] or None,
                        # 폐포사용=False 면 B3 을 조립에 넣지 않는다
                        폐포=(검색결과["폐포"] or None) if 폐포사용 else None,
                        룰결과=b4_문장(룰), f요약=b5_문장(cur, org_id),
                        참조사슬=검색결과["참조사슬"], 변형=변형, 격리근거=격리근거,
                        코드들=코드들)
        잰다("조립", t)
        경로.append("4조립")

        if not s맵:
            # 근거가 한 줄도 없으면 LLM 을 부르지 않는다
            return _마무리(conn, cur, _빈응답(
                "판단불가", "인용할 규정 원문이 없어 판정을 내리지 않았습니다.",
                강등사유=["s맵 0건 — B1·B2·B3 이 모두 비었다"], 강등코드=["NO_CITATION"],
                경로="+".join(경로), 실패단계="조립", 지연ms=지연, **갈림필드), 기록=False, 닫기=닫기)

        if dry and 주입 not in ("schema", "cite"):
            # 드라이런은 여기까지다
            응답 = _빈응답("판단불가", "[dry] LLM 을 부르지 않았다. 프롬프트 조립까지만.",
                        강등사유=[], 강등코드=[], 경로="+".join(경로 + ["dry중단"]))
            응답.update(게이트="D", 비목=비목, 정규화=정규, 검색=검색결과,
                       참조사슬=검색결과["참조사슬"], dry=True,
                       프롬프트길이=len(프롬프트), s맵크기=len(s맵),
                       s맵={k: list(v) for k, v in s맵.items()},
                       b4=bool(룰), b1=len(l3본문), 폐포사용=폐포사용,
                       지연ms={**지연, "총": int((time.time() - t0) * 1000)},
                       모델={"호출수": 0})
            return _마무리(conn, cur, dict(응답, **갈림필드), 기록=False, 닫기=닫기)

        t = time.time()
        try:
            if 주입 == "timeout":
                raise LLM실패("read timeout(주입)")
            스키마 = 판정_스키마(s번호들=list(s맵), 코드들=코드들)
            # 주입 schema/cite 는 LLM 출력을 합성해 (6) 이 잡는지 본다:
            #   schema — 스키마 밖 필드 → INVALID_JUDGMENT
            #   cite   — s맵 밖 S번호   → CITE_NOT_IN_MAP + NO_CITATION
            if 주입 == "schema":
                출력, 메타4 = {"결과": "가능", "이유": "주입"}, {"지연ms": 0, "모델": "합성(주입)"}
            elif 주입 == "cite":
                출력, 메타4 = ({"판정": "가능", "요약": "주입", "해야할일": [],
                              "인용": ["S99"], "전제": []},
                             {"지연ms": 0, "모델": "합성(주입)"})
            else:
                출력, 메타4 = llm_호출(프롬프트, 스키마, 온도=온도, 최대토큰=판정_최대토큰)
        except LLM실패 as e:
            잰다("판정LLM", t)
            return _마무리(conn, cur, _빈응답(
                "판단불가", "판정 모델 호출에 실패해 결론을 내리지 않았습니다.",
                강등사유=[f"(4) LLM 실패: {e}"], 강등코드=[], 경로="+".join(경로),
                실패단계="판정LLM", 지연ms=지연, **갈림필드), 기록=False, 닫기=닫기)
        잰다("판정LLM", t)

        # (6) 검증·강등
        t = time.time()
        _f사실 = b5_값(cur, org_id)          # 한 번만 읽어 (6) 검증과 (5) 전제해소가 같은 값을 본다
        if 주입 == "cite":
            출력 = dict(출력, 인용=["S99"], 전제=[])       # s맵 밖 S번호
        응답, 사유 = 검증(출력, s맵,
                      룰들=(룰 or {}).get("룰들"),
                      체크코드=코드들,
                      현재기관=기관ID, 사업명=사업명,
                      dangling=검색결과["dangling"],
                      l3게이팅=게이팅, 룰=룰,
                      # 층 B — 해야할일 설명 환각 대조
                      f사실=_f사실, 프롬프트=프롬프트,
                      dsn=DSN)
        코드 = 응답.get("강등코드") or []
        잰다("검증", t)
        경로.append("6검증")

        # (5) 전제 해소
        t = time.time()
        # DB 값(_f사실)을 먼저 깔고 화면에서 방금 받은 사용자F값으로 덮는다
        f값 = f값_경로키(_f사실)
        if 사용자F값:
            f값.update(f값_경로키(사용자F값))
        해소 = 전제해소(cur, 응답.get("전제목록") or [], org_id=org_id,
                     사업명=사업명, 비목=비목, 기록=기록,
                     f값=f값)
        잰다("전제해소", t)
        if 해소["미매핑"] and "PREMISE_UNMAPPED" not in 코드:
            코드.append("PREMISE_UNMAPPED")

        응답.update(게이트="D", 경로="+".join(경로), 비목=비목, 정규화=정규,
                   검색=검색결과, 참조사슬=사슬,
                   증빙목록=증빙_발급처(cur, 룰),      # 「결제 후」 화면용
                   전제해소=해소, 강등코드=코드, 강등사유=사유,
                   s맵={k: list(v) for k, v in s맵.items()},
                   지연ms={**지연, "총": int((time.time() - t0) * 1000)},
                   변형=변형, 폐포사용=폐포사용,
                   모델={"호출수": 정규화호출 + 1, "변형": 변형, "정규화": 메타1.get("모델"),
                        # "정규화" 키는 모델 이름 문자열이라 그대로 두고, 상세는 형제 키에 싣는다
                        "정규화메타": {"토큰": 메타1.get("토큰"),
                                     "종료이유": 메타1.get("종료이유"),
                                     "사고흔적있음": 메타1.get("사고흔적있음"),
                                     "사고흔적길이": 메타1.get("사고흔적길이"),
                                     "추론content있음": 메타1.get("추론content있음"),
                                     "추론content길이": 메타1.get("추론content길이")},
                        "판정": 메타4.get("모델"), "판정지연ms": 메타4.get("지연ms"),
                        # 종료이유=="length" 면 그 판단불가는 모델의 선택이 아니라 잘림이다
                        "종료이유": 메타4.get("종료이유"),
                        "토큰": 메타4.get("토큰"),
                        # 사고흔적: content 에 섞인 <think> 를 걷어낸 사실.
                        # 추론content: reasoning_content 로 갈라진 사고
                        "사고흔적있음": 메타4.get("사고흔적있음"),
                        "사고흔적길이": 메타4.get("사고흔적길이"),
                        "추론content있음": 메타4.get("추론content있음"),
                        "추론content길이": 메타4.get("추론content길이"),
                        "요청": {"최대토큰": 판정_최대토큰, "온도": 온도}})
        return _마무리(conn, cur, dict(응답, **갈림필드), 기록=기록, 닫기=닫기, 질문=질문,
                    사업명=사업명, org_id=org_id, 기관ID=기관ID,
                    plan_id=plan_id)

    except 주입실패 as e:
        return _마무리(conn, None, _빈응답(
            "판단불가", "장애가 발생해 판정을 내리지 않았습니다.",
            강등사유=[str(e)], 강등코드=[], 경로="실패", 실패단계="주입",
            지연ms=지연), 기록=False, 닫기=닫기)
    except Exception as e:
        # 예상 못 한 예외도 판단불가로 닫는다. 스택은 남기되 사용자에겐 안 준다
        return _마무리(conn, None, _빈응답(
            "판단불가", "내부 오류로 판정을 내리지 않았습니다.",
            강등사유=[f"{type(e).__name__}: {str(e)[:200]}"], 강등코드=[],
            경로="실패", 실패단계="예외", 트레이스=traceback.format_exc()[-1200:],
            지연ms=지연), 기록=False, 닫기=닫기)


def _병렬_l3(org_id, 사업명, 비목=None):
    """스레드 안에서 자기 커넥션을 연다 — psycopg 커넥션은 공유하지 않는다."""
    if not org_id:
        return [], None
    try:
        # autocommit — 읽기 트랜잭션을 붙들면 다른 세션의 DDL 과 교착난다
        with db.connect(connect_timeout=5, autocommit=True) as c:
            return _l3로드(c.cursor(), org_id, 사업명, 비목), None
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


def _병렬_검색(질문, 사업명, top_k):
    try:
        with db.connect(connect_timeout=5, autocommit=True) as c:
            return _검색(c.cursor(), 질문, 사업명, top_k=top_k), None
    except Exception as e:
        return {"top5": [], "폐포": [], "참조사슬": [], "게이트값": 0.0,
                "dangling": [], "후보수": 0}, f"{type(e).__name__}: {e}"


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


_규정모음버전: str | None = None


def 코퍼스버전(cur) -> str:
    """규정 모음 버전 문자열(`docs{n}/chunks{m}`). `eval.runs` 에 이 키로 들어간다."""
    global _규정모음버전
    if _규정모음버전 is None:
        d, c = cur.execute("SELECT (SELECT count(*) FROM corpus.documents WHERE status='active'), "
                           "(SELECT count(*) FROM corpus.chunks)").fetchone()
        _규정모음버전 = f"docs{d}/chunks{c}"
    return _규정모음버전


# CLI
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
    ap.add_argument("--폐포", choices=["on", "off"], default="on",
                    help="A1 — B3(참조 확장) 을 조립에 넣을지. 기본 on(기존 동작)")
    a = ap.parse_args()
    폐포사용 = a.폐포 != "off"

    print(f"모듈 상태: " + " ".join(f"{k}={'실물' if v else 'STUB'}"
                                for k, v in 모듈상태.items()), file=sys.stderr)

    if a.fault:
        종류 = list(_주입종류) if a.fault == "all" else [a.fault]
        나쁨 = 0
        for f in 종류:
            # dry 로 돈다. vLLM 없이도 각 경로가 각자 제 단계에서 걸려야 한다 —
            # 서버가 없어서 (1) 에서 다 죽으면 아무것도 검증한 게 아니다.
            r = 판정(a.q or "노트북 200만원 구매해도 되나요", 사업명=a.사업명,
                    dry=True, 기록=False, 주입=f, 폐포사용=폐포사용)
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
            # 공통 문항은 `사업명 IS NULL` + `적용범위` 에 원표기가 있다.
            # 사업명='공통...' 을 기대하는 코드는 그 자리에서 0건이 된다.
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
            r = 판정(q, 사업명=사업키, dry=a.dry, 기록=not a.no_log, 변형=a.변형,
                    폐포사용=폐포사용)
            out.append({"gold_id": gid, "세트": 세트, "정답": 정답, **r})
            print(f"{gid:3} [{세트:4}] 게이트={r.get('게이트')} 경로={r.get('경로')} "
                  f"S={r.get('s맵크기', len(r.get('s맵') or {}))} "
                  f"프롬프트={r.get('프롬프트길이', 0):,}자 "
                  f"{r.get('지연ms', {}).get('총', 0):,}ms "
                  + (f"🔴{r.get('실패단계')}" if r.get("실패단계") else ""))
        print(f"\n{len(out)}건 · {time.time()-t0:.0f}초 · 변형={a.변형}")
        if a.eval_log:
            # `설정` 에 사업필터와 변형을 반드시 박는다 — 없으면 이 숫자가 어느 조건에서
            # 나온 건지 못 가린다.
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
                                  "top_k": 5, "온도": 0.0, "폐포사용": 폐포사용},
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
    r = 판정(a.q, 사업명=a.사업명, org_id=a.org_id, dry=a.dry, 기록=not a.no_log,
            폐포사용=폐포사용)
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
