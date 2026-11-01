# -*- coding: utf-8 -*-
"""판정 파이프라인: 정규화 → 룰 조회 → 검색 → 판정 조립 → 판정 호출 → 검증 → 전제 해소 → 기록.

/api/judge(server/main._실_판정)가 판정(...) 을 부른다. LLM 호출은 정규화와 판정 두 번이고 나머지는 코드다.
게이트: A 금지목록 적중(즉시 불가, 판정 LLM 호출 없음), B 검색 결과 0건(판단불가), C 비목 후보가 비슷함(1순위로
판정하고 후보를 응답에 포함), D 정상. 공개: 판정(...), decisions_적재(cur, 행), f값_경로키, 증빙_발급처, 코퍼스버전.
불변식: 모든 실패(DB·타임아웃·스키마·검색 0건·인용 검증 실패·예외)는 판단불가로 반환한다. 쓰는 테이블: tenant.decisions.
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
import retrieve
from _lib import db
from assemble_context import 조립, 프롬프트_변형
from llm_qwen import LLM실패, llm_호출
from llm_schema import 판정_스키마, 체크코드_enum
from llm_validate import 검증, 체크항목_본문
from normalize_run import 정규화
from rule_lookup import effective_rule, l3_게이팅, 금지적중, 비목확정

_log = logging.getLogger(__name__)

# 게이트 B: dense 코사인 최고값이 이 값 미만이면 판단불가. hit/miss 분포가 겹쳐 분리 임계가 없으므로
# 기본 0.0 이고, 실질적으로 top5 가 0건일 때만 적용된다.
게이트B_임계 = float(os.environ.get("SUDDOE_GATE_B", "0.0"))
# 게이트 C: 비목 후보 1·2위가 모두 최소 신뢰도 이상이고 차이가 이 값 미만이면 후보가 비슷하다고 본다.
게이트C_격차 = float(os.environ.get("SUDDOE_GATE_C", "0.15"))
게이트C_최소신뢰 = 0.35
# 판정 호출의 max_tokens 와 온도. decisions.모델.요청 에 같은 값을 기록한다.
판정_최대토큰 = int((os.environ.get("SUDDOE_JUDGE_MAX_TOKENS") or os.environ.get("SUDDOE_판정_최대토큰", "1500")))
판정_온도 = 0.0
# 판정 경로는 사업 필터를 켠다. 끄면 다른 사업의 규정이 인용 근거(B2)에 들어간다.
# retrieve.사업필터_기본 은 검색 평가 기준선을 위해 False 로 두고 여기서만 켠다.
사업필터 = (os.environ.get("SUDDOE_PROGRAM_FILTER") or os.environ.get("SUDDOE_사업필터", "1")) != "0"

# tenant.decisions 에 넣는 컬럼. decisions_적재 는 행의 키 중 이 목록에 있는 것만 넣는다.
_decisions_컬럼: tuple[str, ...] = (
    "org_id", "사업명", "기관id", "질문원문", "plan_id", "정규화", "비목", "금액", "판정",
    "신뢰등급", "요약", "버전스탬프", "인용", "해야할일", "전제", "참조사슬", "미매핑전제",
    "강등사유", "강등코드", "경로", "실패단계", "지연ms", "모델", "검색스냅샷", "코퍼스버전",
)


class 주입실패(Exception):
    """장애 주입(fault injection) 'db' 가 던지는 예외. 실제 연결 실패와 같은 경로로 처리되는지 본다."""


def _빈_검색결과() -> dict:
    return {"top5": [], "폐포": [], "참조사슬": [], "게이트값": 0.0, "dangling": [], "후보수": 0}


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
    t0: float = field(default_factory=time.time)
    지연: dict[str, int] = field(default_factory=dict)
    경로: list[str] = field(default_factory=list)
    conn: Any = None
    cur: Any = None
    # 정규화
    정규: dict = field(default_factory=dict)
    메타1: dict = field(default_factory=dict)
    정규화호출: int = 0
    품목: str = ""
    용도: str = ""
    # 룰 조회
    후보: list[dict] = field(default_factory=list)
    비목: str | None = None
    갈림필드: dict = field(default_factory=dict)
    게이팅: dict = field(default_factory=dict)
    룰: dict | None = None
    # 검색
    검색결과: dict = field(default_factory=_빈_검색결과)
    l3본문: list[dict] = field(default_factory=list)
    # 조립
    코드들: list[str] | None = None
    체크항목: dict[str, dict] = field(default_factory=dict)
    f값: dict | None = None
    프롬프트: str = ""
    s맵: dict = field(default_factory=dict)
    사슬: list[dict] = field(default_factory=list)
    # 판정 호출
    출력: dict = field(default_factory=dict)
    메타4: dict = field(default_factory=dict)

    def _지연기록(self, 이름: str, t: float) -> None:
        self.지연[이름] = int((time.time() - t) * 1000)

    def 지연_총(self) -> dict[str, int]:
        return {**self.지연, "총": int((time.time() - self.t0) * 1000)}


# 응답 만들기와 기록
def _기본응답(판정: str, 요약: str, **추가) -> dict:
    """게이트·실패 응답의 기본 형태. '가능'·'조건부' 는 여기서 나오지 않는다."""
    assert 판정 in ("판단불가", "불가"), f"실패 경로에서 '{판정}' 이 나왔다"
    기본 = dict(판정=판정, 요약=요약, 해야할일=[], 인용목록=[], 전제목록=[],
                신뢰등급=None, 버전스탬프=None, 참조사슬=[], 강등사유=[], 미매핑전제=[])
    기본.update(추가)
    # 판단불가가 모델의 판단인지 파이프라인 실패인지 집계에서 가르는 플래그
    기본["실패경로"] = "실패단계" in 추가
    return 기본


def _실패응답(상태: _판정상태, 단계: str, 요약: str, 사유: str, *,
           강등코드: list[str] | None = None, 경로: str = "실패", **추가) -> dict:
    """파이프라인 실패를 판단불가 응답으로 반환한다. 실패는 decisions 에 기록하지 않는다."""
    응답 = _기본응답("판단불가", 요약, 강등사유=[사유], 강등코드=list(강등코드 or []), 경로=경로,
                   실패단계=단계, 지연ms=상태.지연, **상태.갈림필드)
    응답.update(추가)
    return _기록후종료(상태, 응답, 기록=False)


def _종료(상태: _판정상태, 응답: dict, *, 기록: bool) -> dict:
    """게이트·정상 종료. 비목 후보 갈림 정보를 응답에 포함한다."""
    return _기록후종료(상태, dict(응답, **상태.갈림필드), 기록=기록)


def _기록후종료(상태: _판정상태, 응답: dict, *, 기록: bool) -> dict:
    """`기록` 이면 tenant.decisions 에 한 건 넣고 decision_id 를 응답에 넣는다. 커넥션은 항상 닫는다.

    기록 실패는 판정을 바꾸지 않고 강등사유에 남긴다.
    """
    try:
        if 기록 and 상태.cur is not None:
            응답["decision_id"] = decisions_적재(상태.cur, _decisions_행(상태, 응답))
    except Exception as e:                               # noqa: BLE001
        응답.setdefault("강등사유", []).append(f"(7) decisions 적재 실패: {type(e).__name__}: {e}")
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
        org_id=상태.org_id, 사업명=상태.사업명, 기관id=None, 질문원문=상태.질문,
        plan_id=상태.plan_id,
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
                           "top5": 검색.get("top5") or [],
                           "게이트값": 검색.get("게이트값"),
                           "dangling": 검색.get("dangling") or []},
                          ensure_ascii=False, default=str),
        코퍼스버전=코퍼스버전(상태.cur))


def decisions_적재(cur, 행: dict) -> int | None:
    """`tenant.decisions` 에 한 건 넣고 decision_id 를 돌려준다.

    행의 키 중 _decisions_컬럼 에 있는 것만 넣는다. 넣을 것이 없으면 None. INSERT 오류는 그대로 올린다.
    org_id 가 있는 행은 RLS 정책 때문에 커넥션의 GUC app.org_id 가 같은 값이어야 한다.
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


def _모델메타(정규화호출: int, 메타1: dict, 메타4: dict) -> dict:
    """decisions.모델 에 남기는 호출 메타. 키 이름은 저장된 기록과 비교할 수 있게 유지한다."""
    return {"호출수": 정규화호출 + 1, "변형": 프롬프트_변형, "정규화": 메타1.get("모델"),
            "정규화메타": {"토큰": 메타1.get("토큰"),
                         "종료이유": 메타1.get("종료이유"),
                         "사고흔적있음": 메타1.get("사고흔적있음"),
                         "사고흔적길이": 메타1.get("사고흔적길이"),
                         "추론content있음": 메타1.get("추론content있음"),
                         "추론content길이": 메타1.get("추론content길이")},
            "판정": 메타4.get("모델"), "판정지연ms": 메타4.get("지연ms"),
            # 종료이유 'length' 인 판단불가는 모델의 선택이 아니라 출력이 잘린 것이다
            "종료이유": 메타4.get("종료이유"),
            "토큰": 메타4.get("토큰"),
            "사고흔적있음": 메타4.get("사고흔적있음"),
            "사고흔적길이": 메타4.get("사고흔적길이"),
            "추론content있음": 메타4.get("추론content있음"),
            "추론content길이": 메타4.get("추론content길이"),
            "요청": {"최대토큰": 판정_최대토큰, "온도": 판정_온도}}


# F 값과 전제 해소
def _f값_읽기(cur, org_id) -> tuple[str | None, dict | None]:
    """테넌트 F 값을 한 번 읽어 (B5 프롬프트 문장, 값) 을 돌려준다.

    B5 문장은 f_profile 의 협약 기간·현금 재원 네 값이다(없으면 '미입력'). f_profile 행이 없거나 조회가
    실패하면 None. 값은 None = 세 조회 모두 실패(모른다), {} = F 값이 없다(게스트), 그 밖은 실제 값이다.
    f_personnel·f_exec 는 여러 행이면 한 값으로 정할 수 없어 비운다.
    """
    if not org_id:
        return None, {}
    값: dict = {}
    성공 = False
    B5문장 = None
    try:
        r = cur.execute("""SELECT 협약시작일, 협약종료일, 정부지원_현금, 자기부담_현금,
                                  과업범위요약
                             FROM tenant.f_profile WHERE org_id=%s LIMIT 1""",
                        (org_id,)).fetchone()
        성공 = True
        if r:
            이름 = ("협약시작일", "협약종료일", "정부지원(현금)", "자기부담(현금)")
            B5문장 = "\n".join(f"{n}: {v if v is not None else '미입력'}" for n, v in zip(이름, r[:4]))
            값.update({k: v for k, v in zip(("협약시작일", "협약종료일", "정부지원_현금",
                                            "자기부담_현금", "과업범위요약"), r)
                       if v is not None})
    except Exception:                                    # noqa: BLE001
        _log.exception("F 값 조회 실패(f_profile)")
    for 테이블, 컬럼 in (("f_personnel", ("역할", "고용형태", "타사업참여율", "소속기관유형", "겸직")),
                       ("f_exec", ("비목", "재원", "거래처", "인력역할", "귀속월", "금액"))):
        # f_personnel·f_exec 에는 org_id 가 없어 f_profile.profile_id 로 조인한다
        try:
            행들 = cur.execute(
                f"SELECT {', '.join('t.' + c for c in 컬럼)} FROM tenant.{테이블} t "
                "JOIN tenant.f_profile pr USING (profile_id) WHERE pr.org_id=%s",
                (org_id,)).fetchall()
            성공 = True
            if len(행들) == 1:
                값.update({k: v for k, v in zip(컬럼, 행들[0]) if v is not None})
            elif len(행들) > 1:
                _log.warning("org=%s %s %d행: 한 값으로 정할 수 없어 비운다", org_id, 테이블, len(행들))
        except Exception:                                # noqa: BLE001
            _log.exception("F 값 조회 실패(%s)", 테이블)
    return B5문장, (값 if 성공 else None)


def f값_경로키(값: dict | None) -> dict:
    """F 값의 컬럼명 키를 전제.매핑 경로 키(`F1.정부지원.현금` 형식)로 바꾼다. None 은 {}."""
    if 값 is None:
        return {}
    # 축 번호는 llm_validate.F축_테이블 을 따른다. 과업범위요약 은 f_profile 에 있지만 F2 다
    특례 = {
        "과업범위요약": "F2",
        "역할": "F4", "고용형태": "F4", "타사업참여율": "F4",
        "소속기관유형": "F4", "겸직": "F4",
        "비목": "F3", "재원": "F3", "거래처": "F3",
        "인력역할": "F3", "귀속월": "F3", "금액": "F3",
    }
    return {f'{특례.get(k, "F1")}.{k.replace("_", ".")}': v for k, v in 값.items()}


def 전제해소(전제목록: list[dict], f값: dict | None = None) -> dict:
    """전제를 즉시검증(F 값이 모두 있음) / 인라인요청(일부 없음) / 미매핑(경로 없음·F 스키마 밖) 으로 나눈다."""
    f값 = f값 or {}
    a, b, c = [], [], []
    for p in 전제목록:
        경로 = list(p.get("매핑") or [])
        if p.get("미매핑") or not 경로:
            c.append(p)
        elif all(f값.get(x) is not None for x in 경로):
            a.append(dict(p, 검증됨=True))
        else:
            b.append(dict(p, 필요입력=[x for x in 경로 if f값.get(x) is None]))
    return {"즉시검증": a, "인라인요청": b, "미매핑": c}


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
def _비목후보(룰후보: list[dict], 정규화후보, 고정: str | None) -> list[dict]:
    """비목확정 결과가 없으면 정규화의 비목후보를 쓰고, `고정` 이면 그 비목만 남긴다.

    정규화 후보는 {비목, 신뢰도} dict 목록 또는 문자열(dry 규칙 정규화)이다. 문자열은 신뢰도 0.0 이다.
    고정 비목이 후보에 없으면 다른 비목을 쓰지 않고 그 비목을 새 후보로 만든다.
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
    if 고정:
        후보 = ([c for c in 후보 if c.get("비목") == 고정]
              or [{"비목": 고정, "신뢰도": 0.0, "출처": "사용자지정"}])
    return 후보


def _후보_비슷함(후보: list[dict]) -> bool:
    return (len(후보) >= 2
            and 후보[0].get("신뢰도", 0) >= 게이트C_최소신뢰
            and 후보[1].get("신뢰도", 0) >= 게이트C_최소신뢰
            and (후보[0].get("신뢰도", 0) - 후보[1].get("신뢰도", 0)) < 게이트C_격차)


# 판정 단계. 각 단계는 계속하면 None, 여기서 끝나면 응답 dict 를 돌려준다.
def _정규화단계(상태: _판정상태, 정규화결과: dict | None) -> dict | None:
    t = time.time()
    # 이미 실행한 정규화 결과를 받으면 다시 부르지 않는다(LLM 호출 수 고정)
    if isinstance(정규화결과, dict) and 정규화결과:
        상태.정규, 상태.메타1 = 정규화결과, {"모델": "생략(호출부 제공)", "호출수": 0}
        상태._지연기록("정규화", t)
        상태.경로.append("1정규화(외부)")
    else:
        try:
            if 상태.주입 == "timeout":
                raise LLM실패("read timeout(주입)")
            상태.정규, 상태.메타1 = 정규화(상태.질문, dry=상태.dry)
        except LLM실패 as e:
            상태._지연기록("정규화", t)
            return _실패응답(상태, "정규화",
                          "질문을 정규화하지 못했습니다. 품목과 금액을 나눠 다시 알려주세요.",
                          f"(1) 정규화 실패: {e}")
        상태._지연기록("정규화", t)
        상태.경로.append("1정규화")
        상태.정규화호출 = 0 if 상태.dry else 1
    상태.품목 = 상태.정규.get("품목") or 상태.질문[:40]
    상태.용도 = 상태.정규.get("용도") or ""
    return None


def _룰조회(상태: _판정상태, 고정: str | None) -> dict | None:
    cur = 상태.cur
    t = time.time()
    룰후보 = 비목확정(cur, 상태.품목, 상태.사업명) or []
    상태._지연기록("비목확정", t)
    상태.후보 = _비목후보(룰후보, 상태.정규.get("비목후보"), 고정)
    상태.비목 = 상태.후보[0]["비목"] if 상태.후보 else None

    # 게이트 C: 후보가 비슷해도 1순위로 한 번만 판정하고 후보를 응답에 포함한다. 사용자가 비목 확정 화면에서
    # 고르면 _비목고정 으로 다시 판정한다. 게이트 C 는 경로에만 남기고 게이트 값은 덮어쓰지 않는다
    if _후보_비슷함(상태.후보) and not 고정:
        상태.경로.append("C비목갈림")
        상태.갈림필드 = {"비목갈림": True, "비목후보": 상태.후보[:2]}

    t = time.time()
    l3룰 = l3_load.l3룰(cur, 상태.org_id, 상태.비목) if 상태.org_id else None
    상태.게이팅 = l3_게이팅(l3룰)
    상태._지연기록("l3게이팅", t)

    t = time.time()
    # 값이 None 인 키는 넣지 않는다. 비교할 값이 없었던 경우와 비교가 실패한 경우를 구분한다
    수치 = {k: v for k, v in (("금액", 상태.정규.get("금액")),) if v is not None}
    상태.룰 = (effective_rule(cur, 상태.사업명, 상태.비목, 수치=수치 or None, l3룰=l3룰,
                            하위항목=상태.정규.get("하위항목")) if 상태.비목 else None)
    상태._지연기록("effective_rule", t)

    # 게이트 A: 금지목록 적중이면 판정 LLM 을 부르지 않고 불가로 반환한다
    t = time.time()
    금지 = 금지적중(cur, 상태.품목, 상태.용도, 상태.사업명, 상태.비목)
    상태._지연기록("금지적중", t)
    if not 금지:
        return None
    상태.경로.append("A금지적중")
    응답 = _기본응답("불가", f"금지 항목에 해당합니다 — {금지.get('예시')}",
                   강등사유=[], 강등코드=[], 경로="+".join(상태.경로))
    응답.update(게이트="A", 비목=상태.비목, 정규화=상태.정규, 금지근거=금지,
               지연ms=상태.지연_총(), 모델={"호출수": 상태.정규화호출})
    return _종료(상태, 응답, 기록=상태.기록)


def _l3_스레드(org_id, 비목):
    """L3 조문 전체 로드. psycopg 커넥션은 스레드 간에 공유하지 않으므로 자기 커넥션을 연다."""
    if not org_id:
        return [], None
    try:
        with db.connect(connect_timeout=5, autocommit=True) as c:
            return l3_load.로드(c.cursor(), org_id, 비목=비목) or [], None
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
        # L3 로드와 검색은 서로 독립이라 병렬로 실행한다
        with ThreadPoolExecutor(max_workers=2) as ex:
            fl3 = ex.submit(_l3_스레드, 상태.org_id, 상태.비목)
            fse = ex.submit(_검색_스레드, 상태.질문, 상태.사업명)
            상태.l3본문, l3err = fl3.result()
            상태.검색결과, se_err = fse.result()
        if l3err:
            # 기관 규정 없이 판정하면 L3 가 금지한 지출을 L1·L2 만 보고 '가능' 으로 답할 수 있다
            return _실패응답(상태, "L3로드", "기관 규정을 불러오지 못해 판정을 내리지 않았습니다.",
                          f"(3) L3 로드 실패: {l3err}")
        if se_err:
            return _실패응답(상태, "검색", "규정 검색에 실패해 판정을 내리지 않았습니다.",
                          f"(3) 검색 실패: {se_err}")
    상태._지연기록("검색", t)
    상태.경로.append("3검색")

    # 게이트 B: 검색 결과가 없으면 판단불가. L3 만으로 판단할 수 있으면(상위 규정이 필요 없음) 통과한다
    검색결과 = 상태.검색결과
    l3단독가능 = (not 상태.게이팅.get("need_upper")) and bool(상태.l3본문)
    if (not 검색결과["top5"] or 검색결과["게이트값"] < 게이트B_임계) and not l3단독가능:
        상태.경로.append("B스코어미달")
        응답 = _기본응답("판단불가",
                       "이 질문에 해당하는 규정을 찾지 못했습니다. 담당자 확인이 필요합니다.",
                       강등사유=[f"검색 최고 유사도 {검색결과['게이트값']:.3f} "
                               f"< 임계 {게이트B_임계} · top5 {len(검색결과['top5'])}건"],
                       강등코드=[], 경로="+".join(상태.경로))
        응답.update(게이트="B", 비목=상태.비목, 정규화=상태.정규, 검색=검색결과,
                   참조사슬=검색결과["참조사슬"], 지연ms=상태.지연_총(),
                   모델={"호출수": 상태.정규화호출})
        return _종료(상태, 응답, 기록=상태.기록)
    return None


def _조립단계(상태: _판정상태) -> dict | None:
    cur, conn = 상태.cur, 상태.conn
    t = time.time()
    # 사업명이 없으면 전 사업 공통 check_items code 만 후보가 된다
    상태.코드들 = 체크코드_enum(사업명=상태.사업명, conn=conn) or None
    # check_items 본문은 한 번만 읽어 조립(B5.5)과 검증(해야할일)에 같이 넘긴다
    상태.체크항목 = 체크항목_본문(set(상태.코드들), conn=conn) if 상태.코드들 else {}
    # F 값도 한 번만 읽어 B5 문장과 전제 해소에 같이 쓴다
    B5문장, 상태.f값 = _f값_읽기(cur, 상태.org_id)
    검색결과 = 상태.검색결과
    상태.프롬프트, 상태.s맵, 상태.사슬 = 조립(
        cur, 상태.질문, 상태.정규, l3=상태.l3본문 or None,
        검색=검색결과["top5"] or None,
        참조확장=(검색결과["폐포"] or None) if 상태.참조확장_사용 else None,
        룰결과=str(상태.룰["B4문장"]) if 상태.룰 else None, f요약=B5문장,
        참조사슬=검색결과["참조사슬"], 코드들=상태.코드들, 체크항목=상태.체크항목)
    상태._지연기록("조립", t)
    상태.경로.append("4조립")

    if not 상태.s맵:
        # 인용할 원문이 한 줄도 없으면 판정 LLM 을 부르지 않는다
        return _실패응답(상태, "조립", "인용할 규정 원문이 없어 판정을 내리지 않았습니다.",
                      "s맵 0건 — B1·B2·B3 이 모두 비었다", 강등코드=["NO_CITATION"],
                      경로="+".join(상태.경로))
    if 상태.dry and 상태.주입 not in ("schema", "cite"):
        응답 = _기본응답("판단불가", "[dry] LLM 을 부르지 않았다. 프롬프트 조립까지만.",
                       강등사유=[], 강등코드=[], 경로="+".join(상태.경로 + ["dry중단"]))
        응답.update(게이트="D", 비목=상태.비목, 정규화=상태.정규, 검색=검색결과,
                   참조사슬=검색결과["참조사슬"], dry=True,
                   프롬프트길이=len(상태.프롬프트), s맵크기=len(상태.s맵),
                   s맵={k: list(v) for k, v in 상태.s맵.items()},
                   b4=bool(상태.룰), b1=len(상태.l3본문), 폐포사용=상태.참조확장_사용,
                   지연ms=상태.지연_총(), 모델={"호출수": 0})
        return _종료(상태, 응답, 기록=False)
    return None


def _판정호출(상태: _판정상태) -> dict | None:
    t = time.time()
    try:
        if 상태.주입 == "timeout":
            raise LLM실패("read timeout(주입)")
        스키마 = 판정_스키마(s번호들=list(상태.s맵), 코드들=상태.코드들)
        # 장애 주입 schema/cite 는 LLM 출력을 합성해 검증 단계가 잡는지 본다.
        # schema: 스키마 밖 필드 → INVALID_JUDGMENT, cite: s맵 밖 S번호 → CITE_NOT_IN_MAP + NO_CITATION
        if 상태.주입 == "schema":
            상태.출력, 상태.메타4 = {"결과": "가능", "이유": "주입"}, {"지연ms": 0, "모델": "합성(주입)"}
        elif 상태.주입 == "cite":
            상태.출력, 상태.메타4 = ({"판정": "가능", "요약": "주입", "해야할일": [],
                                  "인용": ["S99"], "전제": []},
                                 {"지연ms": 0, "모델": "합성(주입)"})
        else:
            상태.출력, 상태.메타4 = llm_호출(상태.프롬프트, 스키마, 온도=판정_온도,
                                       최대토큰=판정_최대토큰)
    except LLM실패 as e:
        상태._지연기록("판정LLM", t)
        return _실패응답(상태, "판정LLM", "판정 모델 호출에 실패해 결론을 내리지 않았습니다.",
                      f"(4) LLM 실패: {e}", 경로="+".join(상태.경로))
    상태._지연기록("판정LLM", t)
    return None


def _검증단계(상태: _판정상태, 사용자F값: dict | None) -> dict:
    t = time.time()
    org_id, 룰, 검색결과 = 상태.org_id, 상태.룰, 상태.검색결과
    응답, 사유 = 검증(상태.출력, 상태.s맵,
                  룰들=(룰 or {}).get("룰들"),
                  체크코드=상태.코드들, 체크항목=상태.체크항목,
                  현재기관=str(org_id) if org_id else None, org_id=org_id,
                  conn=상태.conn, 사업명=상태.사업명,
                  dangling=검색결과["dangling"], 룰=룰)
    코드 = 응답.get("강등코드") or []
    상태._지연기록("검증", t)
    상태.경로.append("6검증")

    t = time.time()
    # DB 의 F 값 위에 화면에서 방금 받은 사용자F값을 덮는다
    f값 = f값_경로키(상태.f값)
    if 사용자F값:
        f값.update(f값_경로키(사용자F값))
    해소 = 전제해소(응답.get("전제목록") or [], f값)
    상태._지연기록("전제해소", t)
    if 해소["미매핑"] and "PREMISE_UNMAPPED" not in 코드:
        코드.append("PREMISE_UNMAPPED")

    응답.update(게이트="D", 경로="+".join(상태.경로), 비목=상태.비목, 정규화=상태.정규,
               검색=검색결과, 참조사슬=상태.사슬,
               증빙목록=증빙_발급처(상태.cur, 룰),
               전제해소=해소, 강등코드=코드, 강등사유=사유,
               s맵={k: list(v) for k, v in 상태.s맵.items()},
               지연ms=상태.지연_총(),
               변형=프롬프트_변형, 폐포사용=상태.참조확장_사용,
               모델=_모델메타(상태.정규화호출, 상태.메타1, 상태.메타4))
    return _종료(상태, 응답, 기록=상태.기록)


def 판정(질문: str, *, 사업명: str | None = None, org_id=None, plan_id: int | None = None,
       _비목고정: str | None = None, 정규화결과: dict | None = None,
       사용자F값: dict | None = None,
       dry: bool = False, 기록: bool = True, 주입: str | None = None,
       참조확장_사용: bool = True) -> dict:
    """질문 하나를 판정해 응답 dict 를 돌려준다. 어떤 실패도 예외로 올리지 않고 판단불가로 반환한다.

    서버 인자: `plan_id`(지출계획에 딸린 판정이면 decisions.plan_id), `_비목고정`(사용자가 고른 비목으로 재판정),
    `정규화결과`(이미 실행한 정규화 결과. 주면 정규화 LLM 을 다시 부르지 않는다), `사용자F값`(심층질문 답.
    F 값과 같은 컬럼명 키이며 DB 값보다 우선한다).
    평가용 인자: `dry`(LLM 없이 규칙 정규화와 프롬프트 조립까지만), `기록`(tenant.decisions 기록 여부),
    `주입`(장애 주입 'db'|'timeout'|'schema'|'empty'|'cite'), `참조확장_사용`(False 면 B3 을 넣지 않는다).
    부수 효과: `기록` 이면 게이트·정상 종료를 tenant.decisions 에 넣는다. 실패 종료는 기록하지 않는다.
    """
    상태 = _판정상태(질문=질문, 사업명=사업명, org_id=org_id, plan_id=plan_id, 기록=기록,
                   dry=dry, 주입=주입, 참조확장_사용=참조확장_사용)
    try:
        if 주입 == "db":
            raise 주입실패("DB 연결 실패(주입)")
        # autocommit: LLM 호출 동안 읽기 트랜잭션을 잡고 있으면 다른 세션의 DDL·VACUUM 이 막힌다
        상태.conn = db.connect(connect_timeout=5, autocommit=True)
        # 새 커넥션에는 미들웨어의 GUC 가 없다. RLS 정책이 읽는 app.org_id 를 세션 레벨로 설정해야
        # L3 조회와 decisions INSERT 가 RLS 정책에 의해 거부되지 않는다
        if org_id:
            상태.conn.execute("SELECT set_config('app.org_id', %s, false)", (str(org_id),))
    except Exception as e:                               # noqa: BLE001
        return _실패응답(상태, "DB", "데이터베이스에 연결할 수 없어 판정을 내리지 않았습니다.",
                      f"DB 연결 실패: {type(e).__name__}",
                      지연ms={"총": int((time.time() - 상태.t0) * 1000)})

    try:
        상태.cur = 상태.conn.cursor()
        for 단계 in (lambda: _정규화단계(상태, 정규화결과),
                     lambda: _룰조회(상태, _비목고정),
                     lambda: _검색단계(상태),
                     lambda: _조립단계(상태),
                     lambda: _판정호출(상태)):
            if (응답 := 단계()) is not None:
                return 응답
        return _검증단계(상태, 사용자F값)
    except Exception as e:                               # noqa: BLE001
        # 예상하지 못한 예외도 판단불가로 반환한다. 스택은 응답에 남기고 요약에는 넣지 않는다
        return _실패응답(상태, "예외", "내부 오류로 판정을 내리지 않았습니다.",
                      f"{type(e).__name__}: {str(e)[:200]}",
                      트레이스=traceback.format_exc()[-1200:])


if __name__ == "__main__":
    from eval_e2e import main
    main()
