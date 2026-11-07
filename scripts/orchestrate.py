# -*- coding: utf-8 -*-
"""판정 파이프라인: 정규화 → 룰 조회 → 검색 → 프롬프트 구성 → 판정 호출 → 검증 → 전제 확인 → 저장.

SUDDOE_ONTOLOGY_PROGRAMS 에 있는 사업은 새 경로를 탄다(입구에서 정한다. 한 요청이 두 경로를 섞지 않는다, 5-11 문서):
정규화 → 온톨로지 조회 + 코드 게이트(ontology_gate) → 검색 → 문맥 구성(ontology_context) → 판정 호출 → 검증 → 합치기 → 저장.

/api/judge(server/routes_judge._live_판정)가 판정(...) 을 부른다. 여기서 LLM 호출은 정규화와 판정 두 번이고 나머지는 코드다.
확인 질문 LLM 은 판정 전에 /api/questions(question_generate)가 따로 부르고, 그 답을 사용자답변으로 받는다.
게이트: A 금지목록 일치(즉시 불가, 판정 LLM 호출 없음), B 검색 결과 0건(판단불가), C 비목 후보 근접(1순위로
판정하고 후보를 응답에 포함), D 정상. 공개: 판정(...), decisions_저장(cur, 행), 프로필값_경로키, 증빙_발급처, 코퍼스버전.
불변 조건: 모든 실패(DB·타임아웃·스키마·검색 0건·인용 검증 실패·예외)는 판단불가로 반환한다. 쓰는 테이블: tenant.decisions.
실행(eval_e2e 로 위임): PYTHONIOENCODING=utf-8 python scripts/orchestrate.py --q "맥북 250만원" --dry
"""
from __future__ import annotations

import json
import logging
import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import l3_load
import ontology_context
import ontology_fund
import ontology_gate
import ontology_lookup
import retrieve
from _lib import db
from assemble_context import 프롬프트_구성, 프롬프트_버전
from llm_qwen import LLM실패, llm_호출
from llm_schema import 판정_스키마, 체크코드_enum
from llm_validate import _하향_요약, 검증, 근거_인용, 체크항목_본문
from normalize_run import 정규화
from rule_lookup import effective_rule, l3_상위확인, 금지목록_일치, 비목후보_조회

_log = logging.getLogger(__name__)

# 게이트 B(검색결과 없음): dense 코사인 최고값이 이 값 미만이면 판단불가. hit/miss 분포가 겹쳐 분리 임계가 없으므로
# 기본 0.0 이고, 실질적으로 top5 가 0건일 때만 적용된다.
검색_최소유사도 = float(os.environ.get("SUDDOE_RETRIEVAL_MIN_SIMILARITY")
                   or os.environ.get("SUDDOE_GATE_B", "0.0"))  # 구 이름 호환(제거 예정: 다음 릴리스)
# 게이트 C(비목 후보 근접): 1·2위 후보가 모두 최소 신뢰도 이상이고 차이가 이 값 미만이면 근접으로 본다.
비목후보_최소격차 = float(os.environ.get("SUDDOE_CATEGORY_MIN_MARGIN")
                   or os.environ.get("SUDDOE_GATE_C", "0.15"))  # 구 이름 호환(제거 예정: 다음 릴리스)
비목후보_최소신뢰도 = 0.35
# 판정 호출에 넘기는 max_tokens 와 온도. llm_qwen 이 _최소_출력토큰 보다 작은 값은 올려 보내므로
# decisions.모델.요청.최대토큰 에는 실제로 보낸 값(메타의 최대토큰)을 기록한다.
판정_최대토큰 = int(os.environ.get("SUDDOE_JUDGE_MAX_TOKENS", "1500"))
판정_온도 = 0.0
# 판정 경로는 사업 필터를 켠다. 끄면 다른 사업의 규정이 인용 근거(B2)에 들어간다.
# retrieve.사업필터_기본 은 검색 평가 기준선을 위해 False 로 두고 여기서만 켠다.
사업필터 = os.environ.get("SUDDOE_PROGRAM_FILTER", "1") != "0"

# tenant.decisions 에 넣는 컬럼. decisions_저장 은 행의 키 중 이 목록에 있는 것만 넣는다.
_decisions_컬럼: tuple[str, ...] = (
    "org_id", "account_id", "사업명", "기관id", "질문원문", "plan_id", "정규화", "비목", "금액", "판정",
    "신뢰등급", "요약", "규정버전", "인용", "해야할일", "전제", "참조관계", "미매핑전제",
    "검증사유", "검증코드", "처리단계", "실패단계", "지연ms", "모델", "검색스냅샷", "코퍼스버전",
    "사용자답변", "경로", "온톨로지결과",
)


class 주입실패(Exception):
    """장애 주입(fault injection) 'db' 가 던지는 예외. 실제 연결 실패와 같은 경로로 처리되는지 본다."""


def _빈_검색결과() -> dict:
    return {"top5": [], "참조확장": [], "참조관계": [], "최고유사도": 0.0, "미해결참조": [], "후보수": 0}


@dataclass
class _판정상태:
    """판정 한 건의 요청 값과 단계별 중간 결과."""
    질문: str
    사업명: str | None
    org_id: Any
    plan_id: int | None
    기록: bool
    dry: bool
    주입: str | None
    참조확장_사용: bool
    account_id: Any = None
    # 새 경로: 화면에서 확정한 값. 코드 게이트의 입력은 금액 · 부가세구분 · 추가입력값뿐이다
    새경로: bool = False
    확정비목: str | None = None
    확정세목: str | None = None
    추가입력값: dict = field(default_factory=dict)
    부가세구분: str | None = None
    온톨로지: dict = field(default_factory=dict)
    게이트결과: dict = field(default_factory=dict)
    게이트입력: dict = field(default_factory=dict)
    문맥: dict | None = None
    # 확인 질문 답변(question_generate.답변_구성). 답변유실은 답이 왔는데 저장된 질문을 읽지 못한 경우다
    사용자답변: list[dict] = field(default_factory=list)
    답변유실: bool = False
    t0: float = field(default_factory=time.time)
    지연: dict[str, int] = field(default_factory=dict)
    처리단계: list[str] = field(default_factory=list)
    conn: Any = None
    cur: Any = None
    # 정규화
    정규화결과: dict = field(default_factory=dict)
    정규화_메타: dict = field(default_factory=dict)
    정규화_호출수: int = 0
    품목: str = ""
    용도: str = ""
    # 룰 조회
    후보: list[dict] = field(default_factory=list)
    비목: str | None = None
    비목후보근접_필드: dict = field(default_factory=dict)
    상위확인: dict = field(default_factory=dict)
    룰: dict | None = None
    # 검색
    검색결과: dict = field(default_factory=_빈_검색결과)
    l3본문: list[dict] = field(default_factory=list)
    # 프롬프트 구성
    체크코드: list[str] | None = None
    체크항목: dict[str, dict] = field(default_factory=dict)
    프로필값: dict | None = None
    프롬프트: str = ""
    문장맵: dict = field(default_factory=dict)
    참조관계: list[dict] = field(default_factory=list)
    # 판정 호출
    출력: dict = field(default_factory=dict)
    판정_메타: dict = field(default_factory=dict)

    def _지연기록(self, 이름: str, t: float) -> None:
        self.지연[이름] = int((time.time() - t) * 1000)

    def 지연_총(self) -> dict[str, int]:
        return {**self.지연, "총": int((time.time() - self.t0) * 1000)}


# 응답 만들기와 기록
def _기본응답(판정: str, 요약: str, **추가) -> dict:
    """게이트·실패 응답의 기본 형태. '가능'·'조건부' 는 여기서 나오지 않는다."""
    assert 판정 in ("판단불가", "불가"), f"실패 경로에서 '{판정}' 이 나왔다"
    기본 = dict(판정=판정, 요약=요약, 해야할일=[], 인용목록=[], 전제목록=[],
                신뢰등급=None, 규정버전=None, 참조관계=[], 검증사유=[], 미매핑전제=[])
    기본.update(추가)
    # 판단불가가 모델의 판단인지 파이프라인 실패인지 집계에서 가르는 플래그
    기본["실패경로"] = "실패단계" in 추가
    return 기본


def _실패응답(상태: _판정상태, 단계: str, 요약: str, 사유: str, *,
           검증코드: list[str] | None = None, 처리단계: str = "실패", **추가) -> dict:
    """파이프라인 실패를 판단불가 응답으로 반환한다. 실패는 decisions 에 기록하지 않는다."""
    응답 = _기본응답("판단불가", 요약, 검증사유=[사유], 검증코드=list(검증코드 or []), 처리단계=처리단계,
                   실패단계=단계, 지연ms=상태.지연, **상태.비목후보근접_필드)
    응답.update(추가)
    return _기록후종료(상태, 응답, 기록=False)


def _종료(상태: _판정상태, 응답: dict, *, 기록: bool) -> dict:
    """게이트·정상 종료. 비목 후보 근접 정보를 응답에 포함한다."""
    return _기록후종료(상태, dict(응답, **상태.비목후보근접_필드), 기록=기록)


def _기록후종료(상태: _판정상태, 응답: dict, *, 기록: bool) -> dict:
    """`기록` 이면 tenant.decisions 에 한 건 넣고 decision_id 를 응답에 넣는다. 커넥션은 항상 닫는다.

    기록 실패는 판정을 바꾸지 않고 검증사유에 남긴다. 모든 종료 경로가 지나므로 사용자답변도 여기서 넣는다.
    """
    응답["사용자답변"] = 상태.사용자답변
    try:
        if 기록 and 상태.cur is not None:
            응답["decision_id"] = decisions_저장(상태.cur, _decisions_행(상태, 응답))
    except Exception as e:                               # noqa: BLE001
        응답.setdefault("검증사유", []).append(f"decisions 저장 실패: {type(e).__name__}: {e}")
    finally:
        if 상태.conn is not None:
            try:
                상태.conn.close()
            except Exception:                            # noqa: BLE001
                pass
    return 응답


def _decisions_행(상태: _판정상태, 응답: dict) -> dict:
    검색 = 응답.get("검색") or {}
    return dict(
        # 기관id 는 corpus 문서의 발행 기관 컬럼이라 판정 행에는 쓰지 않는다
        org_id=상태.org_id, account_id=상태.account_id if 상태.org_id else None,
        사업명=상태.사업명, 기관id=None, 질문원문=상태.질문,
        plan_id=상태.plan_id,
        정규화=json.dumps(응답.get("정규화") or {}, ensure_ascii=False),
        비목=응답.get("비목"),
        금액=(응답.get("정규화") or {}).get("금액"),
        판정=응답.get("판정"), 신뢰등급=응답.get("신뢰등급"),
        요약=응답.get("요약"), 규정버전=응답.get("규정버전"),
        인용=json.dumps(응답.get("인용목록") or [], ensure_ascii=False, default=str),
        해야할일=json.dumps(응답.get("해야할일") or [], ensure_ascii=False),
        전제=json.dumps(응답.get("전제목록") or [], ensure_ascii=False, default=str),
        참조관계=json.dumps(응답.get("참조관계") or [], ensure_ascii=False, default=str),
        미매핑전제=json.dumps(응답.get("미매핑전제") or [], ensure_ascii=False),
        검증사유=응답.get("검증사유") or [],
        검증코드=응답.get("검증코드") or [],
        처리단계=응답.get("처리단계"), 실패단계=응답.get("실패단계"),
        지연ms=json.dumps(응답.get("지연ms") or {}, ensure_ascii=False),
        모델=json.dumps(응답.get("모델") or {}, ensure_ascii=False),
        검색스냅샷=json.dumps({"문장맵": 응답.get("문장맵") or {},
                           "top5": 검색.get("top5") or [],
                           "최고유사도": 검색.get("최고유사도"),
                           "미해결참조": 검색.get("미해결참조") or []},
                          ensure_ascii=False, default=str),
        # 충족답·근거조문까지 남긴다. 나중에 질문을 감사할 수 있어야 한다. 화면에는 질문문·답만 나간다
        사용자답변=json.dumps(상태.사용자답변, ensure_ascii=False, default=str),
        코퍼스버전=코퍼스버전(상태.cur),
        경로="온톨로지" if 상태.새경로 else "옛",
        # 계획을 다시 열 때 결과 화면에 그대로 보여 줄 값(server/routes_plans.py _live_상세)
        온톨로지결과=json.dumps({k: 응답.get(k) for k in ("분류", "근거경로", "코드판정", "조건", "금지조건", "해당시조건", "용어풀이")},
                          ensure_ascii=False, default=str) if 상태.새경로 else None)


def decisions_저장(cur, 행: dict) -> int | None:
    """`tenant.decisions` 에 한 건 넣고 decision_id 를 돌려준다.

    행의 키 중 _decisions_컬럼 에 있는 것만 넣는다. 넣을 것이 없으면 None. INSERT 오류는 그대로 올린다.
    org_id 가 있는 행은 RLS 정책 때문에 커넥션의 GUC app.org_id·app.account_id 가 같은 값이어야 한다.
    """
    키 = [k for k in 행 if k in _decisions_컬럼]
    if not 키:
        return None
    q = (f'INSERT INTO tenant.decisions ({",".join(chr(34)+k+chr(34) for k in 키)}) '
         f'VALUES ({",".join(["%s"] * len(키))}) RETURNING decision_id')
    return cur.execute(q, [행[k] for k in 키]).fetchone()[0]


_코퍼스버전_캐시: str | None = None


def 코퍼스버전(cur) -> str:
    """코퍼스 버전 문자열(`docs{n}/chunks{m}`). 프로세스당 한 번 읽는다."""
    global _코퍼스버전_캐시
    if _코퍼스버전_캐시 is None:
        d, c = cur.execute("SELECT (SELECT count(*) FROM corpus.documents WHERE status='active'), "
                           "(SELECT count(*) FROM corpus.chunks)").fetchone()
        _코퍼스버전_캐시 = f"docs{d}/chunks{c}"
    return _코퍼스버전_캐시


def _모델메타(정규화_호출수: int, 정규화_메타: dict, 판정_메타: dict) -> dict:
    """decisions.모델 에 남기는 호출 메타."""
    return {"호출수": 정규화_호출수 + 1, "프롬프트버전": 프롬프트_버전, "정규화": 정규화_메타.get("모델"),
            "정규화메타": {"토큰": 정규화_메타.get("토큰"),
                         "종료이유": 정규화_메타.get("종료이유"),
                         "추론내용있음": 정규화_메타.get("추론내용있음"),
                         "추론내용길이": 정규화_메타.get("추론내용길이")},
            "판정": 판정_메타.get("모델"), "판정지연ms": 판정_메타.get("지연ms"),
            # 종료이유 'length' 인 판단불가는 모델의 선택이 아니라 출력이 잘린 것이다
            "종료이유": 판정_메타.get("종료이유"),
            "토큰": 판정_메타.get("토큰"),
            "추론내용있음": 판정_메타.get("추론내용있음"),
            "추론내용길이": 판정_메타.get("추론내용길이"),
            "요청": {"최대토큰": 판정_메타.get("최대토큰", 판정_최대토큰), "온도": 판정_온도}}


# 기업 프로필 값과 전제 확인
def _프로필값_읽기(cur, org_id, account_id=None) -> tuple[str | None, dict | None]:
    """계정의 기업 프로필 값을 한 번 읽어 (프로필 요약문(B5), 값) 을 돌려준다.

    프로필은 계정 단위다. org_id 와 account_id(데모 세션은 None)가 둘 다 맞는 행만 읽는다(RLS 와 별개로 코드에서도).

    프로필 요약문은 profile_agreements 의 협약 기간·현금 재원 네 값이다(없으면 '미입력'). 행이 없거나 조회가
    실패하면 None. 값은 None = 세 조회 모두 실패(모른다), {} = 프로필 값이 없다(게스트), 그 밖은 실제 값이다.
    profile_personnel·profile_expenditures 는 여러 행이면 한 값으로 정할 수 없어 비운다.
    """
    if not org_id:
        return None, {}
    계정 = str(account_id) if account_id else None
    값: dict = {}
    성공 = False
    프로필요약문 = None
    try:
        r = cur.execute("""SELECT 협약시작일, 협약종료일, 정부지원_현금, 자기부담_현금,
                                  과업범위요약
                             FROM tenant.profile_agreements
                            WHERE org_id=%s AND account_id IS NOT DISTINCT FROM %s::uuid
                            ORDER BY updated_at DESC, profile_id LIMIT 1""",
                        (org_id, 계정)).fetchone()
        성공 = True
        if r:
            이름 = ("협약시작일", "협약종료일", "정부지원(현금)", "자기부담(현금)")
            프로필요약문 = "\n".join(f"{n}: {v if v is not None else '미입력'}" for n, v in zip(이름, r[:4]))
            값.update({k: v for k, v in zip(("협약시작일", "협약종료일", "정부지원_현금",
                                            "자기부담_현금", "과업범위요약"), r)
                       if v is not None})
    except Exception:                                    # noqa: BLE001
        _log.exception("프로필 값 조회 실패(profile_agreements)")
    for 테이블, 컬럼 in (("profile_personnel", ("역할", "고용형태", "타사업참여율", "소속기관유형", "겸직")),
                       ("profile_expenditures", ("비목", "재원", "거래처", "인력역할", "귀속월", "금액"))):
        # profile_personnel·profile_expenditures 에는 org_id 가 없어 profile_agreements.profile_id 로 조인한다
        try:
            행들 = cur.execute(
                f"SELECT {', '.join('t.' + c for c in 컬럼)} FROM tenant.{테이블} t "
                "JOIN tenant.profile_agreements pr USING (profile_id) "
                "WHERE pr.org_id=%s AND pr.account_id IS NOT DISTINCT FROM %s::uuid",
                (org_id, 계정)).fetchall()
            성공 = True
            if len(행들) == 1:
                값.update({k: v for k, v in zip(컬럼, 행들[0]) if v is not None})
            elif len(행들) > 1:
                _log.warning("org=%s %s %d행: 한 값으로 정할 수 없어 비운다", org_id, 테이블, len(행들))
        except Exception:                                # noqa: BLE001
            _log.exception("프로필 값 조회 실패(%s)", 테이블)
    return 프로필요약문, (값 if 성공 else None)


def 프로필값_경로키(값: dict | None) -> dict:
    """프로필 값의 컬럼명 키를 전제.매핑 경로 키(`F1.정부지원.현금` 형식)로 바꾼다. None 은 {}."""
    if 값 is None:
        return {}
    # 접두어는 llm_validate.프로필_테이블 을 따른다. 과업범위요약 은 profile_agreements 에 있지만 F2 다
    특례 = {
        "과업범위요약": "F2",
        "역할": "F4", "고용형태": "F4", "타사업참여율": "F4",
        "소속기관유형": "F4", "겸직": "F4",
        "비목": "F3", "재원": "F3", "거래처": "F3",
        "인력역할": "F3", "귀속월": "F3", "금액": "F3",
    }
    return {f'{특례.get(k, "F1")}.{k.replace("_", ".")}': v for k, v in 값.items()}


def 전제확인(전제목록: list[dict], 프로필값: dict | None = None) -> dict:
    """전제를 확인됨(프로필 값이 모두 있음) / 입력필요(일부 없음) / 미매핑(경로 없음·프로필 스키마 밖) 으로 나눈다."""
    프로필값 = 프로필값 or {}
    a, b, c = [], [], []
    for p in 전제목록:
        경로 = list(p.get("매핑") or [])
        if p.get("미매핑") or not 경로:
            c.append(p)
        elif all(프로필값.get(x) is not None for x in 경로):
            a.append(dict(p, 검증됨=True))
        else:
            b.append(dict(p, 필요입력=[x for x in 경로 if 프로필값.get(x) is None]))
    return {"확인됨": a, "입력필요": b, "미매핑": c}


def 증빙_발급처(cur, 룰: dict | None) -> list[dict]:
    """룰의 증빙 이름 목록에 `corpus.evidence_sources.발급처` 를 붙인다(결제 후 화면용). 모르는 이름은 발급처 None."""
    이름들 = list((룰 or {}).get("증빙") or [])
    if not 이름들:
        return []
    cur.execute(
        'SELECT "증빙명", "발급처" FROM corpus.evidence_sources WHERE "증빙명" = ANY(%s)',
        (이름들,))
    발급처 = {r[0]: r[1] for r in cur.fetchall()}
    return [{"증빙명": n, "발급처": 발급처.get(n)} for n in 이름들]


# 비목 후보
def _비목후보(룰후보: list[dict], 정규화후보, 확정: str | None) -> list[dict]:
    """비목후보_조회 결과가 없으면 정규화의 비목후보를 쓰고, `확정` 이면 그 비목만 남긴다.

    정규화 후보는 {비목, 신뢰도} dict 목록 또는 문자열(dry 규칙 정규화)이다. 문자열은 신뢰도 0.0 이다.
    확정 비목이 후보에 없으면 다른 비목을 쓰지 않고 그 비목을 새 후보로 만든다.
    """
    후보 = list(룰후보 or [])
    if not 후보 and 정규화후보:
        if isinstance(정규화후보, (str, dict)):
            정규화후보 = [정규화후보]
        elif not isinstance(정규화후보, (list, tuple)):
            정규화후보 = []                  # dict 를 순회하면 키가 비목이 되므로 막는다
        for c in 정규화후보:
            if isinstance(c, str):
                이름, 신뢰 = c, 0.0
            elif isinstance(c, dict):
                이름, 신뢰 = c.get("비목"), c.get("신뢰도", 0.0)
            else:
                continue
            if 이름:
                후보.append({"비목": 이름, "신뢰도": 신뢰, "출처": "정규화"})
    if 확정:
        후보 = ([c for c in 후보 if c.get("비목") == 확정]
              or [{"비목": 확정, "신뢰도": 0.0, "출처": "사용자지정"}])
    return 후보


def _후보_근접(후보: list[dict]) -> bool:
    return (len(후보) >= 2
            and 후보[0].get("신뢰도", 0) >= 비목후보_최소신뢰도
            and 후보[1].get("신뢰도", 0) >= 비목후보_최소신뢰도
            and (후보[0].get("신뢰도", 0) - 후보[1].get("신뢰도", 0)) < 비목후보_최소격차)


# 판정 단계. 각 단계는 계속하면 None, 여기서 끝나면 응답 dict 를 돌려준다.
def _정규화단계(상태: _판정상태, 외부_정규화결과: dict | None) -> dict | None:
    t = time.time()
    # 이미 실행한 정규화 결과를 받으면 다시 부르지 않는다(LLM 호출 수 고정)
    if isinstance(외부_정규화결과, dict) and 외부_정규화결과:
        상태.정규화결과, 상태.정규화_메타 = 외부_정규화결과, {"모델": "생략(호출부 제공)", "호출수": 0}
        상태._지연기록("정규화", t)
        상태.처리단계.append("정규화(재사용)")
    else:
        try:
            if 상태.주입 == "timeout":
                raise LLM실패("read timeout(주입)")
            상태.정규화결과, 상태.정규화_메타 = 정규화(상태.질문, dry=상태.dry)
        except LLM실패 as e:
            상태._지연기록("정규화", t)
            return _실패응답(상태, "정규화",
                          "질문을 해석하지 못했습니다. 품목과 금액을 나눠 다시 알려주세요.",
                          f"정규화 실패: {e}")
        상태._지연기록("정규화", t)
        상태.처리단계.append("정규화")
        상태.정규화_호출수 = 0 if 상태.dry else 1
    상태.품목 = 상태.정규화결과.get("품목") or 상태.질문[:40]
    상태.용도 = 상태.정규화결과.get("용도") or ""
    return None


def _룰조회(상태: _판정상태, 확정: str | None) -> dict | None:
    cur = 상태.cur
    t = time.time()
    룰후보 = 비목후보_조회(cur, 상태.품목, 상태.사업명) or []
    상태._지연기록("비목후보조회", t)
    상태.후보 = _비목후보(룰후보, 상태.정규화결과.get("비목후보"), 확정)
    상태.비목 = 상태.후보[0]["비목"] if 상태.후보 else None

    # 게이트 C: 후보가 근접해도 1순위로 한 번만 판정하고 후보를 응답에 포함한다. 사용자가 비목 확정 화면에서
    # 고르면 확정비목 으로 다시 판정한다. 게이트 C 는 처리단계에만 남기고 게이트 값은 덮어쓰지 않는다
    if _후보_근접(상태.후보) and not 확정:
        상태.처리단계.append("비목후보근접")
        상태.비목후보근접_필드 = {"비목후보근접": True, "비목후보": 상태.후보[:2]}

    t = time.time()
    l3룰 = l3_load.l3룰(cur, 상태.org_id, 상태.비목) if 상태.org_id else None
    상태.상위확인 = l3_상위확인(l3룰)
    상태._지연기록("L3상위확인", t)

    t = time.time()
    # 값이 None 인 키는 넣지 않는다. 비교할 값이 없었던 경우와 비교가 실패한 경우를 구분한다
    수치 = {k: v for k, v in (("금액", 상태.정규화결과.get("금액")),) if v is not None}
    상태.룰 = (effective_rule(cur, 상태.사업명, 상태.비목, 수치=수치 or None, l3룰=l3룰,
                            하위항목=상태.정규화결과.get("하위항목")) if 상태.비목 else None)
    상태._지연기록("effective_rule", t)

    # 게이트 A: 금지목록과 일치하면 판정 LLM 을 부르지 않고 불가로 반환한다
    t = time.time()
    금지 = 금지목록_일치(cur, 상태.품목, 상태.용도, 상태.사업명, 상태.비목)
    상태._지연기록("금지목록조회", t)
    if not 금지:
        return None
    상태.처리단계.append("금지목록일치")
    응답 = _기본응답("불가", f"금지 항목에 해당합니다 — {금지.get('예시')}",
                   검증사유=[], 검증코드=[], 처리단계="+".join(상태.처리단계))
    응답.update(게이트="A", 비목=상태.비목, 정규화=상태.정규화결과, 금지근거=금지,
               지연ms=상태.지연_총(), 모델={"호출수": 상태.정규화_호출수})
    return _종료(상태, 응답, 기록=상태.기록)


def _l3_스레드(org_id, 비목):
    """L3 조문 전체 조회. psycopg 커넥션은 스레드 간에 공유하지 않으므로 자기 커넥션을 연다."""
    if not org_id:
        return [], None
    try:
        with db.connect(connect_timeout=5, autocommit=True) as c:
            return l3_load.조문_조회(c.cursor(), org_id, 비목=비목) or [], None
    except Exception as e:                               # noqa: BLE001
        return [], f"{type(e).__name__}: {e}"


def _검색_스레드(질문, 사업명):
    try:
        with db.connect(connect_timeout=5, autocommit=True) as c:
            return retrieve.검색(c.cursor(), 질문, 사업명, top_k=5, 사업필터=사업필터), None
    except Exception as e:                               # noqa: BLE001
        return _빈_검색결과(), f"{type(e).__name__}: {e}"


def _검색단계(상태: _판정상태) -> dict | None:
    t = time.time()
    if 상태.주입 == "empty":
        상태.검색결과, 상태.l3본문 = _빈_검색결과(), []
    else:
        # L3 조회와 검색은 서로 독립이라 병렬로 실행한다
        with ThreadPoolExecutor(max_workers=2) as ex:
            fl3 = ex.submit(_l3_스레드, 상태.org_id, 상태.비목)
            fse = ex.submit(_검색_스레드, 상태.질문, 상태.사업명)
            상태.l3본문, l3err = fl3.result()
            상태.검색결과, se_err = fse.result()
        if l3err:
            # 기관 자체규정 없이 판정하면 L3 가 금지한 지출을 L1·L2 만 보고 '가능' 으로 답할 수 있다
            return _실패응답(상태, "L3로드", "기관 규정을 불러오지 못해 판정을 내리지 않았습니다.",
                          f"L3 로드 실패: {l3err}")
        if se_err:
            return _실패응답(상태, "검색", "규정 검색에 실패해 판정을 내리지 않았습니다.",
                          f"검색 실패: {se_err}")
    상태._지연기록("검색", t)
    상태.처리단계.append("검색")

    # 게이트 B: 검색 결과가 없으면 판단불가. L3 만으로 판단할 수 있으면(상위 규정이 필요 없음) 통과한다
    검색결과 = 상태.검색결과
    l3단독가능 = (not 상태.상위확인.get("need_upper")) and bool(상태.l3본문)
    if (not 검색결과["top5"] or 검색결과["최고유사도"] < 검색_최소유사도) and not l3단독가능:
        상태.처리단계.append("검색유사도미달")
        응답 = _기본응답("판단불가",
                       "이 질문에 해당하는 규정을 찾지 못했습니다. 담당자 확인이 필요합니다.",
                       검증사유=[f"검색 최고 유사도 {검색결과['최고유사도']:.3f} "
                               f"< 임계 {검색_최소유사도} · top5 {len(검색결과['top5'])}건"],
                       검증코드=[], 처리단계="+".join(상태.처리단계))
        응답.update(게이트="B", 비목=상태.비목, 정규화=상태.정규화결과, 검색=검색결과,
                   참조관계=검색결과["참조관계"], 지연ms=상태.지연_총(),
                   모델={"호출수": 상태.정규화_호출수})
        return _종료(상태, 응답, 기록=상태.기록)
    return None


def _답변_근거조문(cur, 사용자답변: list[dict], 검색_청크: list[int]) -> list[int]:
    """사용자가 답한 질문의 근거 조문 article_id. 판정이 그 조문을 인용할 수 있게 B3 에 더한다.

    청크는 doc_id·조번호로 조문을 찾는다. L3 는 B1 에 전체가 있어 더하지 않는다. 근거 항이 이미 B2 청크에
    있어도 더하지 않는다(발췌는 줄을 다듬은 값이라 글자로 비교하지 않는다).
    """
    if not 사용자답변:
        return []
    cur.execute('SELECT doc_id, "조번호", "항호" FROM corpus.chunks WHERE chunk_id = ANY(%s)', (검색_청크,))
    b2 = {tuple(r) for r in cur.fetchall()}
    out: list[int] = []
    for a in 사용자답변:
        g, 근거 = a.get("근거조문") or {}, a.get("근거") or {}
        조 = (근거.get("doc_id"), 근거.get("조번호"))
        if (*조, 근거.get("항호") or None) in b2 or (*조, None) in b2:
            continue
        if g.get("종류") == "article" and g.get("id"):
            out.append(g["id"])
        elif g.get("종류") == "chunk":
            cur.execute('SELECT article_id FROM corpus.doc_articles WHERE doc_id = %s AND "조번호" = %s',
                        (g.get("doc_id"), g.get("조번호")))
            out += [r[0] for r in cur.fetchall()]
    return out


def _프롬프트구성단계(상태: _판정상태) -> dict | None:
    cur, conn = 상태.cur, 상태.conn
    t = time.time()
    # 사업명이 없으면 전 사업 공통 check_items code 만 후보가 된다
    상태.체크코드 = 체크코드_enum(사업명=상태.사업명, conn=conn) or None
    # check_items 본문은 한 번만 읽어 프롬프트 구성(B5.5)과 검증(해야할일)에 같이 넘긴다
    상태.체크항목 = 체크항목_본문(set(상태.체크코드), conn=conn) if 상태.체크코드 else {}
    # 프로필 값도 한 번만 읽어 프로필 요약문(B5)과 전제 확인에 같이 쓴다
    프로필요약문, 상태.프로필값 = _프로필값_읽기(cur, 상태.org_id, 상태.account_id)
    검색결과 = 상태.검색결과
    참조확장 = (검색결과["참조확장"] or None) if 상태.참조확장_사용 else None
    if 답변조문 := _답변_근거조문(cur, 상태.사용자답변, 검색결과["top5"]):
        참조확장 = list(dict.fromkeys([*(참조확장 or []), *답변조문]))
    상태.프롬프트, 상태.문장맵, 상태.참조관계 = 프롬프트_구성(
        cur, 상태.질문, 상태.정규화결과, l3=상태.l3본문 or None,
        검색=검색결과["top5"] or None,
        참조확장=참조확장,
        룰결과=str(상태.룰["룰요약문"]) if 상태.룰 else None, 프로필요약=프로필요약문,
        참조관계=검색결과["참조관계"], 체크코드=상태.체크코드, 체크항목=상태.체크항목,
        사용자답변=상태.사용자답변 or None)
    상태._지연기록("프롬프트구성", t)
    상태.처리단계.append("판정생성")

    if not 상태.문장맵:
        # 인용할 원문이 한 줄도 없으면 판정 LLM 을 부르지 않는다
        return _실패응답(상태, "프롬프트구성", "인용할 규정 원문이 없어 판정을 내리지 않았습니다.",
                      "문장맵 0건 — B1·B2·B3 이 모두 비었다", 검증코드=["NO_CITATION"],
                      처리단계="+".join(상태.처리단계))
    if 상태.dry and 상태.주입 not in ("schema", "cite"):
        응답 = _기본응답("판단불가", "[dry] LLM 을 호출하지 않았다. 프롬프트 구성까지만 실행했다.",
                       검증사유=[], 검증코드=[], 처리단계="+".join(상태.처리단계 + ["dry종료"]))
        응답.update(게이트="D", 비목=상태.비목, 정규화=상태.정규화결과, 검색=검색결과,
                   참조관계=검색결과["참조관계"], dry=True,
                   프롬프트길이=len(상태.프롬프트), 문장맵_크기=len(상태.문장맵),
                   문장맵={k: list(v) for k, v in 상태.문장맵.items()},
                   b4=bool(상태.룰), b1=len(상태.l3본문), 참조확장사용=상태.참조확장_사용,
                   지연ms=상태.지연_총(), 모델={"호출수": 0})
        return _종료(상태, 응답, 기록=False)
    return None


def _판정호출(상태: _판정상태) -> dict | None:
    t = time.time()
    try:
        if 상태.주입 == "timeout":
            raise LLM실패("read timeout(주입)")
        스키마 = 판정_스키마(문장ID들=list(상태.문장맵), 체크코드=상태.체크코드)
        # 장애 주입 schema/cite 는 LLM 출력을 합성해 검증 단계가 잡는지 본다.
        # schema: 스키마 밖 필드 → INVALID_JUDGMENT, cite: 문장맵 밖 문장 ID → CITE_NOT_IN_MAP + NO_CITATION
        if 상태.주입 == "schema":
            상태.출력, 상태.판정_메타 = {"결과": "가능", "이유": "주입"}, {"지연ms": 0, "모델": "합성(주입)"}
        elif 상태.주입 == "cite":
            상태.출력, 상태.판정_메타 = ({"판정": "가능", "요약": "주입", "해야할일": [],
                                  "인용": ["S9999"], "전제": []},
                                 {"지연ms": 0, "모델": "합성(주입)"})
        else:
            상태.출력, 상태.판정_메타 = llm_호출(상태.프롬프트, 스키마, 온도=판정_온도,
                                       최대토큰=판정_최대토큰)
    except LLM실패 as e:
        상태._지연기록("판정호출", t)
        return _실패응답(상태, "판정호출", "판정 모델 호출에 실패해 결론을 내리지 않았습니다.",
                      f"판정 호출 실패: {e}", 처리단계="+".join(상태.처리단계))
    상태._지연기록("판정호출", t)
    return None


def _모름_할일(사용자답변: list[dict]) -> list[dict]:
    """「모름」 답마다 결제 전 확인 항목을 만든다. check_items code 가 없어 plan_tasks.코드 는 NULL 이고,
    문구가 다른 항목으로 오매칭되지 않게 코드 추정을 막는다. 같은 문구는 한 번만 넣는다."""
    문구 = {a["할일"]: a.get("이유") for a in 사용자답변 if a.get("답") == "모름" and a.get("할일")}
    return [{"항목": 항목, "설명": 이유, "구분": "결제전", "코드추정_제외": True} for 항목, 이유 in 문구.items()]


def _검증단계(상태: _판정상태, 사용자프로필값: dict | None) -> dict:
    t = time.time()
    org_id, 룰, 검색결과 = 상태.org_id, 상태.룰, 상태.검색결과
    응답, 사유 = 검증(상태.출력, 상태.문장맵,
                  룰들=(룰 or {}).get("룰들"),
                  체크코드=상태.체크코드, 체크항목=상태.체크항목,
                  현재기관=str(org_id) if org_id else None, org_id=org_id,
                  conn=상태.conn, 사업명=상태.사업명,
                  미해결참조=검색결과["미해결참조"],
                  사용자답변=상태.사용자답변, 답변유실=상태.답변유실)
    응답["해야할일"] = [*(응답.get("해야할일") or []), *_모름_할일(상태.사용자답변)]
    코드 = 응답.get("검증코드") or []
    상태._지연기록("검증", t)
    상태.처리단계.append("검증")

    t = time.time()
    # DB 의 프로필 값 위에 화면에서 방금 받은 사용자프로필값을 덮는다
    프로필값 = 프로필값_경로키(상태.프로필값)
    if 사용자프로필값:
        프로필값.update(프로필값_경로키(사용자프로필값))
    확인결과 = 전제확인(응답.get("전제목록") or [], 프로필값)
    상태._지연기록("전제확인", t)
    if 확인결과["미매핑"] and "PREMISE_UNMAPPED" not in 코드:
        코드.append("PREMISE_UNMAPPED")

    응답.update(게이트="D", 처리단계="+".join(상태.처리단계), 비목=상태.비목, 정규화=상태.정규화결과,
               검색=검색결과, 참조관계=상태.참조관계,
               증빙목록=증빙_발급처(상태.cur, 룰),
               전제확인=확인결과, 검증코드=코드, 검증사유=사유,
               문장맵={k: list(v) for k, v in 상태.문장맵.items()},
               지연ms=상태.지연_총(),
               프롬프트버전=프롬프트_버전, 참조확장사용=상태.참조확장_사용,
               모델=_모델메타(상태.정규화_호출수, 상태.정규화_메타, 상태.판정_메타))
    return _종료(상태, 응답, 기록=상태.기록)


# 새 경로(온톨로지). 각 단계는 계속하면 None, 여기서 끝나면 응답 dict 를 돌려준다.
def 게이트_입력(금액, 부가세구분: str | None, 추가입력값: dict | None) -> dict:
    """코드 게이트 입력. 화면이 준 값만 쓴다. LLM 이 뽑은 수치는 비교에 쓰지 않는다(칸 미리 채우기용)."""
    값 = {k: v for k, v in (추가입력값 or {}).items() if k in ontology_gate.입력키 and v not in (None, "")}
    return {"금액": 금액, "부가세구분": 부가세구분 or "모름", **값}


def 온톨로지_조회(cur, 사업명: str | None, 비목: str, 세목: str | None, 입력: dict, *, 확정: bool,
              품목: str | None = None) -> dict:
    """대상 노드 → 적용 규범 → 근거 조문 → 코드 게이트. 판정과 질문 생성이 같은 값을 쓴다. 조회 오류는 그대로 올린다.

    품목 표현이 확정 노드 아래 품목과 정확히 일치하면 그 품목을 시작 노드로 삼는다(품목 금지 · 상위 규범 상속).
    일치한 품목이 확정 세목 밖이지만 같은 비목(또는 모든 비목) 아래면 그 규범을 `적용["경로밖_품목규범"]` 으로 둔다:
    발췌 · 근거 조는 문맥에 넣고 금지는 조건 문장으로 알리되 코드 불가는 내지 않는다(5-6 ③).
    """
    대상 = ontology_lookup.대상_노드(cur, 사업명, 비목, 세목)
    품목_id = ontology_lookup.품목_노드(cur, 사업명, 품목, 대상["대상_id"]) if 확정 else None
    시작 = 품목_id or 대상["대상_id"]
    적용 = ontology_lookup.적용_규범(cur, 사업명, 시작)
    노드들 = {시작, *(p for _, p in 적용["경로"]), ontology_lookup.모든비목}
    자금 = ontology_fund.경로_자금(사업명, 대상["비목_id"])
    적용["규범"], 공통미정 = ontology_fund.공통규범_나누기(자금, 적용["규범"], 노드들)
    적용["공통미정"] = 공통미정
    밖품목 = ontology_lookup.경로밖_품목(cur, 사업명, 품목, 대상["비목_id"]) if 확정 and not 품목_id else []
    밖규범 = ontology_lookup.경로밖_규범(cur, 사업명, 밖품목, {"규범": [*적용["규범"], *공통미정]})
    적용["경로밖_품목규범"], 적용["경로밖_품목"] = 밖규범, 밖품목
    # 품목 표현이 어느 별칭과도 일치하지 않으면 이 비목의 금지 품목을 판정 문맥에 대조 재료로 넣는다. 유사 일치를 코드로 하지 않고 코드 불가도 내지 않는다
    품목표: list[dict] = []
    if 확정 and 품목 and not 품목_id and not 밖품목:
        비목들 = [대상["비목_id"], *([] if 자금 == ontology_fund.창업활동자금 else [ontology_lookup.모든비목])]
        품목표 = [n for n in ontology_lookup.비목_품목금지(cur, 사업명, 비목들) if ontology_gate.경로밖금지_구분(n, None) == "묶음"]
    적용["품목표"] = 품목표
    금지미정_id = ontology_fund.금지문장_조문ID(자금)
    적용["금지미정"] = bool(금지미정_id)
    schema = ontology_lookup._스키마()
    문서키표 = {v: k for k, v in (schema.get("문서키") or {}).items()}
    근거행 = ontology_fund.근거행_거르기(ontology_lookup.근거_조문(cur, [*적용["규범"], *밖규범], 3, 시작, 사업=사업명), 자금, 문서키표)
    실효 = [n for n in 적용["규범"] if n["종류"] != "우선순위"]
    위임만 = not 실효 and ontology_lookup.위임_있음(근거행)
    세목미정 = 확정 and not 세목 and not 품목_id and ontology_lookup.세목_한도_있음(cur, 사업명, 대상["대상_id"])
    공통품목 = ontology_fund.공통_품목규범(자금, 밖규범)
    게이트결과 = ontology_gate.게이트(적용["규범"], 입력, 확정=확정, 세목표시=세목,
                              우선순위_적용=적용["우선순위_적용"], 위임만=위임만, 세목미정=세목미정,
                              문서표시=ontology_context.문서_표시명(schema),
                              경로밖_품목규범=[n for n in 밖규범 if n not in 공통품목], 경로밖_품목=밖품목,
                              # 금지 문장 항은 규범 데이터와 상관없이 적용 미정이다. 결정 전에는 '가능' 을 내지 않는다
                              공통미정=[*공통미정, *공통품목, *({"종류": "금지", "norm_id": i} for i in 금지미정_id)],
                              문의처=ontology_fund.문의처(자금))
    if 공통미정:                         # 문맥에는 뒤쪽 블록으로 넣되 화면 근거 경로에는 쓰지 않는다. 준용은 따라가지 않는다
        근거행 = [*근거행, *({**행, "공통미정": True} for 행 in ontology_fund.근거행_거르기(
            ontology_lookup.근거_조문(cur, 공통미정, 0, None, 사업=사업명), 자금, 문서키표))]
    if 품목표:
        근거행 = [*근거행, *({**행, "품목표": True} for 행 in ontology_fund.근거행_거르기(
            ontology_lookup.근거_조문(cur, 품목표, 0, None, 사업=사업명), 자금, 문서키표))]
    근거행 = [*근거행, *({**행, "공통미정": True} for 행 in ontology_lookup.조문_행(cur, 금지미정_id, "금지미정"))]
    return {"대상": 대상, "적용": 적용, "근거행": 근거행, "게이트": 게이트결과, "노드들": 노드들, "자금": 자금}


def _새_문의_안내(종류: str) -> str:
    return {"금지": "규정상 집행할 수 없는 항목입니다.",
            "부가세기준미정": "부가세 포함 여부에 따라 결과가 달라져 판정을 내리지 않았습니다. 입력한 금액의 부가세 포함 여부를 알려 주세요."}[종류]


def _코드판정_요약(g: dict, 부가세구분: str | None) -> str:
    종류, 코드판정 = g["종류"], g.get("코드판정") or {}
    if 종류 == "한도초과":
        return f"{코드판정.get('비교값')}으로 한도({코드판정.get('한도')})를 넘어 집행할 수 없습니다."
    if 종류 == "부가세기준미정" and 부가세구분 in ("포함", "공급가액"):
        return "규정에 금액의 부가세 기준이 적혀 있지 않아 판정을 내리지 않았습니다. 주관기관에 확인해 주세요."
    return _새_문의_안내(종류)


def _표시_분류(대상: dict | None) -> dict | None:
    return {k: 대상.get(k) for k in ("비목", "세목", "표시문")} if 대상 else None


def _온톨로지단계(상태: _판정상태) -> dict | None:
    t = time.time()
    if 상태.주입 == "onto":
        raise 주입실패("온톨로지 조회 실패(주입)")
    비목, 세목 = 상태.확정비목, 상태.확정세목
    확정 = bool(비목)
    if not 비목:                 # 화면 밖 경로(CLI 등). 추천값으로 문맥만 만들고 게이트는 발동하지 않는다
        후보 = 상태.정규화결과.get("분류후보") or []
        if 후보:
            비목, 세목 = 후보[0].get("비목"), 후보[0].get("세목")
    상태.비목 = 비목
    입력 = 상태.게이트입력 = 게이트_입력(상태.정규화결과.get("금액"), 상태.부가세구분, 상태.추가입력값)
    if 비목:
        try:
            상태.온톨로지 = 온톨로지_조회(상태.cur, 상태.사업명, 비목, 세목, 입력, 확정=확정,
                                      품목=상태.정규화결과.get("품목"))
        except LookupError as e:
            return _실패응답(상태, "온톨로지", "고른 분류를 규정에서 찾지 못해 판정을 내리지 않았습니다. 분류를 다시 골라 주세요.",
                          f"온톨로지 조회 실패: {e}")
        상태.게이트결과 = 상태.온톨로지["게이트"]
    else:
        상태.게이트결과 = ontology_gate.게이트([], 입력, 확정=False)
        상태.온톨로지 = {"대상": None, "적용": {"규범": [], "경로": [], "우선순위_적용": []}, "근거행": [],
                      "게이트": 상태.게이트결과, "노드들": set()}
    상태._지연기록("온톨로지", t)
    상태.처리단계.append("온톨로지")
    g = 상태.게이트결과
    if not g.get("종류"):
        return None

    # 코드 판정: 판정 LLM 을 부르지 않는다
    상태.처리단계.append(f"코드판정:{g['종류']}")
    최종, _ = ontology_gate.합치기(g, None)
    발동 = g.get("발동규범") or {}
    인용목록 = 근거_인용(ontology_context.코드판정_문장맵(상태.cur, 발동), conn=상태.conn, org_id=상태.org_id,
                     현재기관=str(상태.org_id) if 상태.org_id else None)
    근거행 = ontology_lookup.근거_조문(상태.cur, [발동], 3, None, 사업=상태.사업명)
    # 문의 초안(server/inquiry.py)이 무엇을 물을지 알도록 부가세 기준 미정을 전제로 남긴다
    전제 = [{"사실": "이 금액 기준을 부가세 포함 금액으로 볼지 공급가액으로 볼지", "근거조항": None, "매핑": [], "미충족시": "판단불가"}] \
        if g["종류"] == "부가세기준미정" else []
    응답 = _기본응답(최종, _코드판정_요약(g, 입력.get("부가세구분")), 인용목록=인용목록, 전제목록=전제,
                   검증사유=[], 검증코드=list(g["검증코드"]), 처리단계="+".join(상태.처리단계))
    응답.update(게이트="A" if g["종류"] == "금지" else "코드", 비목=비목, 정규화=상태.정규화결과,
               코드판정=g["코드판정"], 근거경로=ontology_context.근거경로들(근거행, ontology_lookup._스키마(), 규범들=[발동]),
               분류=_표시_분류(상태.온톨로지["대상"]), 조건=[], 금지조건=[], 해당시조건=[], 경로="온톨로지", 지연ms=상태.지연_총(),
               모델={"호출수": 상태.정규화_호출수})
    return _종료(상태, 응답, 기록=상태.기록)


def _새_l3_스레드(org_id):
    if not org_id:
        return [], None
    try:
        with db.connect(connect_timeout=5, autocommit=True) as c:
            return l3_load.조문_연결조회(c.cursor(), org_id) or [], None
    except Exception as e:                               # noqa: BLE001
        return [], f"{type(e).__name__}: {e}"


def _새_검색_스레드(질의, 사업명):
    try:
        with db.connect(connect_timeout=5, autocommit=True) as c:
            return retrieve.검색(c.cursor(), 질의, 사업명, top_k=10, 사업필터=사업필터), None
    except Exception as e:                               # noqa: BLE001
        return _빈_검색결과(), f"{type(e).__name__}: {e}"


def _새_검색단계(상태: _판정상태) -> dict | None:
    """L3 + 검색 후보(상위 10) + 문맥 구성. 온톨로지 후보 · L3 · 검색이 모두 비면 판단불가(게이트 B)."""
    t = time.time()
    if 상태.주입 == "empty":
        상태.검색결과, 상태.l3본문 = _빈_검색결과(), []
        상태.온톨로지 = {**상태.온톨로지, "근거행": []}
    else:
        질의 = 상태.정규화결과.get("질의문") or 상태.질문
        with ThreadPoolExecutor(max_workers=2) as ex:
            fl3 = ex.submit(_새_l3_스레드, 상태.org_id)
            fse = ex.submit(_새_검색_스레드, 질의, 상태.사업명)
            상태.l3본문, l3err = fl3.result()
            상태.검색결과, se_err = fse.result()
        if l3err:
            return _실패응답(상태, "L3로드", "기관 규정을 불러오지 못해 판정을 내리지 않았습니다.", f"L3 로드 실패: {l3err}")
        if se_err:
            return _실패응답(상태, "검색", "규정 검색에 실패해 판정을 내리지 않았습니다.", f"검색 실패: {se_err}")
    상태._지연기록("검색", t)
    상태.처리단계.append("검색")

    t = time.time()
    검색결과 = {**상태.검색결과, "참조확장": 상태.검색결과["참조확장"] if 상태.참조확장_사용 else []}
    상태.문맥 = ontology_context.문맥_구성(상태.cur, 근거행=상태.온톨로지["근거행"], 대상노드=상태.온톨로지["노드들"],
                                     l3=상태.l3본문, 검색=검색결과, schema=ontology_lookup._스키마(),
                                     자금=상태.온톨로지.get("자금"))
    상태._지연기록("문맥구성", t)
    if 상태.문맥["L3_초과"]:
        상태.처리단계.append("L3상한초과")
        응답 = _기본응답("판단불가",
                       "주관기관 규정이 길어 한 번에 볼 수 없어 판정을 내리지 않았습니다. 이 지출을 집행해도 되는지 주관기관에 확인해 주세요.",
                       검증사유=[f"L3 미연결 조문이 글자 상한을 넘음(뺀 항목 {상태.문맥['빠진수']}개)"],
                       검증코드=[], 처리단계="+".join(상태.처리단계))
        응답.update(게이트="B", 비목=상태.비목, 정규화=상태.정규화결과, 검색=상태.검색결과,
                   코드판정=None, 근거경로=[], 분류=_표시_분류(상태.온톨로지["대상"]), 조건=[], 금지조건=[], 해당시조건=[], 경로="온톨로지",
                   지연ms=상태.지연_총(), 모델={"호출수": 상태.정규화_호출수})
        return _종료(상태, 응답, 기록=상태.기록)
    if not 상태.문맥["항목수"]:
        상태.처리단계.append("후보없음")
        응답 = _기본응답("판단불가", "이 질문에 해당하는 규정을 찾지 못했습니다. 담당자 확인이 필요합니다.",
                       검증사유=["온톨로지 · L3 · 검색 후보 0건"], 검증코드=[], 처리단계="+".join(상태.처리단계))
        응답.update(게이트="B", 비목=상태.비목, 정규화=상태.정규화결과, 검색=상태.검색결과,
                   코드판정=None, 근거경로=[], 분류=_표시_분류(상태.온톨로지["대상"]), 조건=[], 금지조건=[], 해당시조건=[], 경로="온톨로지",
                   지연ms=상태.지연_총(), 모델={"호출수": 상태.정규화_호출수})
        return _종료(상태, 응답, 기록=상태.기록)
    return None


def _새_프롬프트단계(상태: _판정상태) -> dict | None:
    cur = 상태.cur
    t = time.time()
    # 새 경로의 할 일은 의무 규범에서만 만든다(5-5, 5-6 ⑥). check_items 는 자금 구분이 없어 선불카드로 쓰는 창업활동자금에도
    # "사업비 카드로 결제" 를 붙인다. 그래서 체크코드 · 체크항목을 비워 둔다(해야할일 스키마 maxItems 0, B5.5 없음)
    프로필요약문, 상태.프로필값 = _프로필값_읽기(cur, 상태.org_id, 상태.account_id)
    대상 = 상태.온톨로지["대상"]
    b4 = ontology_context.규범_요약(상태.온톨로지["적용"], 상태.게이트결과, 분류표시=(대상 or {}).get("표시문"),
                               확정=bool(상태.확정비목), l3있음=bool(상태.l3본문))
    상태.프롬프트, 상태.문장맵, 상태.참조관계 = 프롬프트_구성(
        cur, 상태.질문, 상태.정규화결과, 룰결과=b4, 프로필요약=프로필요약문,
        참조관계=상태.검색결과["참조관계"], 체크코드=상태.체크코드, 체크항목=상태.체크항목,
        사용자답변=상태.사용자답변 or None, 문맥=상태.문맥)
    상태._지연기록("프롬프트구성", t)
    상태.처리단계.append("판정생성")
    if not 상태.문장맵:
        return _실패응답(상태, "프롬프트구성", "인용할 규정 원문이 없어 판정을 내리지 않았습니다.",
                      "문장맵 0건", 검증코드=["NO_CITATION"], 처리단계="+".join(상태.처리단계))
    if 상태.dry and 상태.주입 not in ("schema", "cite"):
        응답 = _기본응답("판단불가", "[dry] LLM 을 호출하지 않았다. 프롬프트 구성까지만 실행했다.",
                       검증사유=[], 검증코드=[], 처리단계="+".join(상태.처리단계 + ["dry종료"]))
        응답.update(게이트="D", 비목=상태.비목, 정규화=상태.정규화결과, 검색=상태.검색결과,
                   참조관계=상태.검색결과["참조관계"], dry=True, 프롬프트=상태.프롬프트,
                   프롬프트길이=len(상태.프롬프트), 문장맵_크기=len(상태.문장맵),
                   문장맵={k: list(v) for k, v in 상태.문장맵.items()},
                   b4=bool(b4), b1=len(상태.l3본문), 참조확장사용=상태.참조확장_사용, 경로="온톨로지",
                   근거경로=ontology_context.근거경로들(상태.온톨로지["근거행"], ontology_lookup._스키마(), 상한=None, 규범들=상태.온톨로지["적용"]["규범"]),
                   **_화면_칸(ontology_gate.화면_조건(상태.게이트결과)), 분류=_표시_분류(대상),
                   문맥요약={k: 상태.문맥[k] for k in ("항목수", "글자수", "빠진수", "온톨로지항목수")},
                   지연ms=상태.지연_총(), 모델={"호출수": 0})
        return _종료(상태, 응답, 기록=False)
    return None


_시점_구분 = {"집행 전": "결제전", "집행전": "결제전", "기한": "결제전", "집행 후": "결제후", "집행후": "결제후"}


def _증빙_안내(cur, 조건들: list[dict]) -> dict[str, str]:
    """의무의 증빙 노드 → 「세금계산서: 국세청 홈택스」 형식 설명. 발급처를 아는 증빙만 적는다.

    증빙 목록은 택일(세금계산서 또는 카드 영수증)을 담지 못해 할일을 증빙별로 나누지 않는다. 필수 여부는 의무 문장이 말한다.
    """
    ids = sorted({i for c in 조건들 for i in c.get("증빙") or []})
    if not ids:
        return {}
    발급처 = dict(cur.execute(
        """SELECT c.concept_id, c.pref_label || ': ' || e."발급처"
             FROM corpus.concepts c JOIN corpus.evidence_sources e ON e."증빙명" = c.pref_label
            WHERE c.concept_id = ANY(%s) AND e."발급처" IS NOT NULL""", (ids,)).fetchall())
    return {c["문장"]: " · ".join(발급처[i] for i in c.get("증빙") or [] if i in 발급처)
            for c in 조건들 if any(i in 발급처 for i in c.get("증빙") or [])}


def _화면_칸(조건들: list[dict]) -> dict[str, list[str]]:
    """게이트 조건 → 응답 칸 `조건` · `금지조건` · `해당시조건`(문장 목록). 소비: server/judge_response.py, routes_plans.py."""
    return {칸: [c["문장"] for c in 줄] for 칸, 줄 in ontology_gate.화면_조건_나누기(조건들).items()}


def _조건_할일(조건들: list[dict], 증빙안내: dict[str, str] | None = None) -> list[dict]:
    """코드 게이트의 조건 → 해야할일 항목. check_items code 가 없어 코드 추정을 막는다(_모름_할일 과 같다). 해당할 때만 걸리는 의무는 할 일이 아니다."""
    return [{"항목": c["문장"], "설명": (증빙안내 or {}).get(c["문장"]),
             "구분": _시점_구분.get(c.get("시점") or "", "결제전"), "코드추정_제외": True}
            for c in 조건들 if c.get("구분") in ("의무", "금지조건", "한도예외", "위임", "두기준", "미확정규범", "미확정한도") and not c.get("해당시")]


_요약금액_문구 = {
    "판단불가": "판정 설명의 금액이 입력한 금액이나 규정의 금액과 맞지 않아 판정을 내리지 않았습니다. 담당자 확인이 필요합니다.",
    "불가": "규정상 집행할 수 없다고 판단했습니다. 아래 근거 조항을 살펴봐 주세요.",
}


def _새_검증단계(상태: _판정상태, 사용자프로필값: dict | None) -> dict:
    t = time.time()
    org_id, 검색결과, g = 상태.org_id, 상태.검색결과, 상태.게이트결과
    규범들 = 상태.온톨로지["적용"]["규범"]
    # 미확정 규범만 있으면 UNVERIFIED_RULE 로 내린다. 우선순위 규범은 판정 근거 규범이 아니라 세지 않는다
    룰들 = [{"verified": bool(n.get("verified")) and not n.get("해석미결")} for n in 규범들 if n["종류"] != "우선순위"]
    응답, 사유 = 검증(상태.출력, 상태.문장맵, 룰들=룰들 or None,
                  체크코드=상태.체크코드, 체크항목=상태.체크항목,
                  현재기관=str(org_id) if org_id else None, org_id=org_id,
                  conn=상태.conn, 사업명=상태.사업명,
                  미해결참조=검색결과["미해결참조"],
                  사용자답변=상태.사용자답변, 답변유실=상태.답변유실)
    # 요약에 입력 · 규범 · 인용 원문 어디에도 없는 금액이 있으면 그 판단을 믿지 않는다(숫자는 코드, 5-6 불변식). 어긋난 금액은 화면에 내지 않는다
    틀린금액 = ontology_gate.요약_금액_불일치(
        응답.get("요약"), 상태.게이트입력, [*규범들, *상태.온톨로지["적용"].get("경로밖_품목규범", []), *상태.온톨로지["적용"].get("품목표", [])],
        [상태.질문, 상태.정규화결과.get("품목"), 상태.정규화결과.get("용도"), *(a.get("질문문") for a in 상태.사용자답변),
         *(c.get("원문") for c in 응답.get("인용목록") or [])])
    if 틀린금액:
        판정 = 응답.get("판정")
        응답.update(판정=판정 if 판정 == "불가" else "판단불가", 요약=_요약금액_문구["불가" if 판정 == "불가" else "판단불가"])
        사유.append(f"요약의 금액 {sorted(틀린금액)} 이 입력 · 규범 · 인용 원문에 없음 → {응답['판정']}")
        상태.처리단계.append("요약금액불일치")
    # 합치는 규칙: 코드 결과는 상한이다. '가능' 만 조건부로 내린다
    최종, 낮춤 = ontology_gate.합치기(g, 응답.get("판정"))
    if 낮춤:
        응답.update(판정=최종, 요약=_하향_요약["조건부"])
        사유.append("코드 결과(의무 · 입력 비움 · 미확정 규범 · 위임) → '가능' 을 조건부로 하향")
        상태.처리단계.append("코드상한")
    조건 = ontology_gate.화면_조건(g)
    할일 = _조건_할일(조건, _증빙_안내(상태.cur, 조건)) if 응답.get("판정") != "불가" else []
    응답["해야할일"] = [*(응답.get("해야할일") or []), *할일, *_모름_할일(상태.사용자답변)]
    코드 = 응답.get("검증코드") or []
    상태._지연기록("검증", t)
    상태.처리단계.append("검증")

    t = time.time()
    프로필값 = 프로필값_경로키(상태.프로필값)
    if 사용자프로필값:
        프로필값.update(프로필값_경로키(사용자프로필값))
    확인결과 = 전제확인(응답.get("전제목록") or [], 프로필값)
    상태._지연기록("전제확인", t)
    if 확인결과["미매핑"] and "PREMISE_UNMAPPED" not in 코드:
        코드.append("PREMISE_UNMAPPED")
    응답.update(게이트="D", 처리단계="+".join(상태.처리단계), 비목=상태.비목, 정규화=상태.정규화결과,
               검색=검색결과, 참조관계=상태.참조관계, 증빙목록=[],
               전제확인=확인결과, 검증코드=코드, 검증사유=사유,
               문장맵={k: list(v) for k, v in 상태.문장맵.items()},
               지연ms=상태.지연_총(), 프롬프트버전=프롬프트_버전, 참조확장사용=상태.참조확장_사용,
               모델=_모델메타(상태.정규화_호출수, 상태.정규화_메타, 상태.판정_메타),
               코드판정=None, 경로="온톨로지",
               근거경로=ontology_context.근거경로들(상태.온톨로지["근거행"], ontology_lookup._스키마(), 규범들=규범들),
               분류=_표시_분류(상태.온톨로지["대상"]), **_화면_칸(조건 if 응답.get("판정") != "불가" else []))
    return _종료(상태, 응답, 기록=상태.기록)


def 판정(질문: str, *, 사업명: str | None = None, org_id=None, account_id=None,
       plan_id: int | None = None,
       확정비목: str | None = None, 정규화결과: dict | None = None,
       사용자프로필값: dict | None = None,
       사용자답변: list[dict] | None = None, 답변유실: bool = False,
       확정세목: str | None = None, 추가입력값: dict | None = None, 부가세구분: str | None = None,
       dry: bool = False, 기록: bool = True, 주입: str | None = None,
       참조확장_사용: bool = True) -> dict:
    """질문 하나를 판정해 응답 dict 를 돌려준다. 어떤 실패도 예외로 올리지 않고 판단불가로 반환한다.

    서버 인자: `account_id`(토큰으로 검증된 계정. 프로필(B5)·decisions 는 계정 단위이고 L3 는 org 단위다. 데모 세션은
    None), `plan_id`(지출계획에 딸린 판정이면 decisions.plan_id), `확정비목`(사용자가 고른 비목으로 재판정),
    `정규화결과`(이미 실행한 정규화 결과. 주면 정규화 LLM 을 다시 부르지 않는다), `사용자프로필값`(심층질문 답.
    프로필 값과 같은 컬럼명 키이며 DB 값보다 우선한다), `사용자답변`(확인 질문 답. 프롬프트 B5.7 과 검증 하향에 쓴다),
    `답변유실`(답이 왔는데 저장된 질문을 읽지 못함. 「가능」을 내지 않는다).
    새 경로 인자: `확정세목`(화면에서 확정한 세목 표기), `추가입력값`(화면 숫자 칸), `부가세구분`(포함 · 공급가액 · 모름).
    평가용 인자: `dry`(LLM 없이 규칙 정규화와 프롬프트 구성까지만), `기록`(tenant.decisions 기록 여부),
    `주입`(장애 주입 'db'|'timeout'|'schema'|'empty'|'cite'|'onto'), `참조확장_사용`(False 면 B3 을 넣지 않는다).
    부수 효과: `기록` 이면 게이트·정상 종료를 tenant.decisions 에 넣는다. 실패 종료는 기록하지 않는다.
    """
    상태 = _판정상태(질문=질문, 사업명=사업명, org_id=org_id, account_id=account_id,
                   plan_id=plan_id, 기록=기록,
                   dry=dry, 주입=주입, 참조확장_사용=참조확장_사용,
                   사용자답변=list(사용자답변 or []), 답변유실=답변유실,
                   확정비목=확정비목, 확정세목=확정세목, 추가입력값=dict(추가입력값 or {}), 부가세구분=부가세구분)
    try:
        if 주입 == "db":
            raise 주입실패("DB 연결 실패(주입)")
        # autocommit: LLM 호출 동안 읽기 트랜잭션을 잡고 있으면 다른 세션의 DDL·VACUUM 이 막힌다
        상태.conn = db.connect(connect_timeout=5, autocommit=True)
        # 새 커넥션에는 미들웨어의 GUC 가 없다. RLS 정책이 읽는 app.org_id·app.account_id 를 세션 레벨로
        # 설정해야 L3 조회·프로필 조회·decisions INSERT 가 RLS 정책에 의해 거부되지 않는다
        if org_id:
            상태.conn.execute("SELECT set_config('app.org_id', %s, false), "
                             "set_config('app.account_id', %s, false)",
                             (str(org_id), str(account_id) if account_id else ""))
    except Exception as e:                               # noqa: BLE001
        return _실패응답(상태, "DB", "데이터베이스에 연결할 수 없어 판정을 내리지 않았습니다.",
                      f"DB 연결 실패: {type(e).__name__}",
                      지연ms={"총": int((time.time() - 상태.t0) * 1000)})

    try:
        상태.cur = 상태.conn.cursor()
        상태.새경로 = ontology_lookup.새경로_대상(상태.cur, 사업명)
        if 상태.새경로:
            for 단계 in (lambda: _정규화단계(상태, 정규화결과),
                         lambda: _온톨로지단계(상태),
                         lambda: _새_검색단계(상태),
                         lambda: _새_프롬프트단계(상태),
                         lambda: _판정호출(상태)):
                if (응답 := 단계()) is not None:
                    return 응답
            return _새_검증단계(상태, 사용자프로필값)
        for 단계 in (lambda: _정규화단계(상태, 정규화결과),
                     lambda: _룰조회(상태, 확정비목),
                     lambda: _검색단계(상태),
                     lambda: _프롬프트구성단계(상태),
                     lambda: _판정호출(상태)):
            if (응답 := 단계()) is not None:
                return 응답
        return _검증단계(상태, 사용자프로필값)
    except Exception as e:                               # noqa: BLE001
        # 예상하지 못한 예외도 판단불가로 반환한다. 스택은 응답에 남기고 요약에는 넣지 않는다
        return _실패응답(상태, "예외", "내부 오류로 판정을 내리지 않았습니다.",
                      f"{type(e).__name__}: {str(e)[:200]}",
                      트레이스=traceback.format_exc()[-1200:])


if __name__ == "__main__":
    from eval_e2e import main
    main()
