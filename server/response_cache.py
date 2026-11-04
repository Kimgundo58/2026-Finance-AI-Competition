# -*- coding: utf-8 -*-
"""정규화·판정·확인 질문 응답 캐시. 같은 입력이 다시 오면 LLM 을 부르지 않고 저장된 응답을 돌려준다.

/api/normalize·/api/judge·/api/questions 가 캐시키() 로 키를 만들고 조회(k, org, account)·저장(k, 값, 종류=, org=, account=) 를
부른다. 게스트(org 없음)는 프로세스 메모리(FIFO, 재시작하면 사라진다), 인증된 org 는 tenant.judge_cache 를 쓴다.
DB 캐시 행은 계정 단위다(account_id, RLS account_isolation). 데모 세션은 계정이 없어 account_id 가 NULL 이다.
두 캐시 모두 _데이터버전_해시()(코퍼스·룰·우선순위 규칙·체크항목·별칭 버전)가 저장 때와 같을 때만 적중한다.
확인 질문은 키가 곧 질문세트이고 저장(ttl초=) 로 항목별 만료 시각을 준다. 질문세트_조회() 는 조회() 와 달리 일일
카운트를 되돌리지 않는다. /api/questions 의 캐시 확인(카운트를 올리기 전)과 판정 요청 안의 조회가 쓴다.
환경변수: SUDDOE_RESPONSE_CACHE_TTL(기본 900초), SUDDOE_RESPONSE_CACHE_MAX(메모리 항목 수, 기본 512),
SUDDOE_DATA_VERSION_TTL(기본 300초). 이전 변수 이름도 한 릴리스 동안 읽는다.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any

from server._common import MOCK, _jsonb, _계정조건, _실행, _정수_환경변수, _질의, ttl_캐시
from server.rate_limit import 제한

_TTL초 = _정수_환경변수("SUDDOE_RESPONSE_CACHE_TTL", 900, 구이름="SUDDOE_CACHE_TTL")  # 구 이름 호환(제거 예정: 다음 릴리스)
_상한 = _정수_환경변수("SUDDOE_RESPONSE_CACHE_MAX", 512, 구이름="SUDDOE_CACHE_MAX")  # 구 이름 호환(제거 예정: 다음 릴리스)

# 게스트용 메모리 캐시(키 -> (만료 시각, 데이터 버전 해시, 값))와 적중 수. AnyIO 워커 스레드가 동시에 바꾸므로
# _락 안에서만 다룬다.
_메모리: dict[str, tuple[float, str, Any]] = {}
_적중 = 0
_락 = threading.Lock()


def 캐시키(*조각: Any) -> str:
    """조각들의 sha256. 원문·특수관계 값을 메모리에 들고 있지 않으려고 해시만 키로 쓴다.

    호출부는 org_id 와 account_id 를 조각에 넣는다. 빠지면 A 기관(계정)의 판정이 B 기관(계정)에 나간다.
    판정 응답에는 그 계정의 계획·판정 id 와 프로필 값으로 만든 전제가 들어 있다.
    """
    raw = json.dumps(조각, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def l3_해시(org: str | None) -> str:
    """기관 L3 문서 집합과 파싱 상태의 해시. 판정 캐시키에 넣는다. 게스트·mock·DB 오류면 빈 문자열.

    L3 파싱이 커밋되면 파싱품질이 'pending' 에서 바뀌므로 해시가 바뀐다. 요청 시작 때 계산하므로,
    파싱 커밋 전에 시작해 옛 L3 로 판정한 요청은 옛 해시 키로 저장되어 이후 요청에 적중하지 않는다.
    """
    if org is None or MOCK:
        return ""
    행 = _질의('SELECT md5(coalesce(string_agg(doc_id::text || \':\' || coalesce("파싱품질", \'\') '
              '|| \':\' || coalesce(status, \'\'), \'#\' ORDER BY doc_id), \'\')) '
              'FROM tenant.l3_documents WHERE org_id = %s', (org,))
    return 행[0][0] if 행 else ""


def 프로필_해시(org: str | None, account: str | None = None) -> str:
    """계정 협약 프로필(tenant.profile_agreements) 내용의 해시. 판정 캐시키에 넣는다. 게스트·mock·DB 오류면 빈 문자열.

    협약값은 판정 프롬프트 B5 에 들어간다. PUT /api/profile 로 값이 바뀌면 키가 바뀌어 이전 판정이 적중하지 않는다.
    updated_at 은 넣지 않는다(같은 값을 다시 저장해도 캐시를 버리지 않게). account 가 None 이면 계정 없는 행(데모)이다.
    """
    if org is None or MOCK:
        return ""
    조건, 인자 = _계정조건(org, "p", account)
    행 = _질의('SELECT md5(coalesce(string_agg(concat_ws(\'|\', profile_id, "사업명", "협약시작일", '
              '"협약종료일", "정부지원_현금", "자기부담_현금", "과업범위요약"), \'#\' ORDER BY profile_id), \'\')) '
              f'FROM tenant.profile_agreements p WHERE {조건}', 인자)
    return 행[0][0] if 행 else ""


def 조회(k: str, org: str | None, account: str | None = None) -> Any | None:
    """저장된 응답 또는 None. 적중하면 일일 판정 카운트를 되돌린다(LLM 을 부르지 않았다).

    org·account 는 검증된 값(auth.검증된_org·검증된_account)을 넘긴다. 다른 계정이 저장한 행은 읽지 않는다.
    """
    global _적중
    값 = _메모리_조회(k) if org is None else _db_조회(k, org, account)
    if 값 is not None:
        with _락:
            _적중 += 1
        제한.카운트_복원()
    return 값


def 질문세트_조회(k: str, org: str | None, account: str | None = None) -> Any | None:
    """저장된 질문세트 또는 None. 조회() 와 같은 기준으로 읽되 일일 판정 카운트를 되돌리지 않는다.

    판정 요청 안에서 읽을 때 되돌리면 LLM 을 부른 그 판정이 카운트에서 빠진다. 다른 계정이 저장한 행은 읽지 않는다.
    """
    return _메모리_조회(k) if org is None else _db_조회(k, org, account)


def 저장(k: str, 값: Any, *, 종류: str, org: str | None, account: str | None = None,
         데이터버전_해시: str | None = None, ttl초: int | None = None) -> None:
    """응답을 저장한다. DB 쓰기 실패는 삼킨다(캐시는 판정 결과에 영향을 주지 않는다).

    데이터버전_해시 는 요청 시작 때 요청_해시() 로 계산한 값을 넘긴다. 저장 시점에 다시 계산하면, 규정
    활성화 전에 시작해 옛 규정으로 판정한 결과가 새 해시로 저장되어 이후 요청에 적중한다.
    ttl초 를 주지 않으면 SUDDOE_RESPONSE_CACHE_TTL 을 쓴다.
    """
    ttl초 = _TTL초 if ttl초 is None else ttl초
    if 데이터버전_해시 is None:
        데이터버전_해시 = _게스트_해시() if org is None else _데이터버전_해시()
    if org is None:
        해시 = 데이터버전_해시
        with _락:
            if len(_메모리) >= _상한:          # FIFO. LRU 까지는 필요 없다
                _메모리.pop(next(iter(_메모리), None), None)
            _메모리[k] = (time.time() + ttl초, 해시, 값)
        return
    # 같은 key 가 다른 계정 행이면 RLS 가 UPDATE 를 거부해 저장이 실패한다(캐시만 못 쓴다). 키에 계정이 들어가
    # 있어 정상 경로에서는 생기지 않는다.
    _실행(
        'INSERT INTO tenant.judge_cache (key, org_id, account_id, 종류, value, 데이터버전_해시, expires_at) '
        'VALUES (%s,%s,%s,%s,%s,%s, now() + make_interval(secs => %s)) '
        'ON CONFLICT (key) DO UPDATE SET '
        '  value = EXCLUDED.value, 데이터버전_해시 = EXCLUDED.데이터버전_해시, '
        '  expires_at = EXCLUDED.expires_at, created_at = now()',
        (k, org, account, 종류, _jsonb(값), 데이터버전_해시, ttl초))


def 요청_해시() -> str:
    """요청 시작 때의 데이터 버전 해시. 판정 라우트가 검색 전에 계산해 저장(데이터버전_해시=) 로 넘긴다."""
    return _게스트_해시()


def 데이터버전_해시_무효화() -> None:
    """데이터 버전 해시의 프로세스 캐시(SUDDOE_DATA_VERSION_TTL)를 지운다. 규정 활성화 직후에 부른다."""
    _데이터버전_해시_조회.무효화()


def 상태() -> dict:
    """/admin/status 용. 메모리 캐시 항목 수·TTL·적중 수(메모리+DB)."""
    with _락:
        return {"항목": len(_메모리), "TTL초": _TTL초, "적중": _적중}


def _게스트_해시() -> str:
    """게스트 캐시용 데이터 버전 해시. mock 모드는 DB 가 없으므로 비교하지 않는다(빈 문자열)."""
    return "" if MOCK else _데이터버전_해시()


def _메모리_조회(k: str) -> Any | None:
    # 규정 활성화·룰 수정으로 데이터 버전 해시가 바뀌면 TTL 이 남았어도 버린다(DB 캐시와 같은 기준).
    해시 = _게스트_해시()
    with _락:
        v = _메모리.get(k)
        if not v:
            return None
        만료시각, 저장해시, 값 = v
        if time.time() > 만료시각 or 저장해시 != 해시:
            _메모리.pop(k, None)
            return None
        return 값


def _db_조회(k: str, org: str, account: str | None) -> Any | None:
    # 게스트는 여기 오지 않는다. RLS 정책은 org_id = current_org() 라 org 가 NULL 이면 통과하지 못한다.
    조건, 인자 = _계정조건(org, "c", account)
    행 = _질의(
        'SELECT value FROM tenant.judge_cache c '
        f'WHERE c.key=%s AND {조건} AND c.expires_at > now() AND c.데이터버전_해시=%s',
        (k, *인자, _데이터버전_해시()))
    return 행[0][0] if 행 else None


# 캐시 무효화 기준. 코퍼스·룰·우선순위 규칙·체크항목·별칭이 바뀌면 값이 바뀌어 기존 DB 행이 미스가 된다.
# 행 수만으로는 행 수를 그대로 둔 컬럼 단위 UPDATE 를 못 잡으므로, 판정에 쓰이는 룰 컬럼은
# rule_id 순으로 이어붙여 md5 한다. TEXT[] 컬럼은 원소 순서가 달라도 같은 값이 나오게 정렬한다.
_데이터버전_해시_SQL = """SELECT (SELECT count(*) FROM corpus.chunks),
                        (SELECT count(*) FROM corpus.chunks WHERE embedding IS NOT NULL),
                        (SELECT count(*) FROM corpus.refs),
                        (SELECT count(*) FROM corpus.documents),
                        (SELECT coalesce(max(chunk_id), 0) FROM corpus.chunks),
                        (SELECT count(*) FROM corpus.rules),
                        (SELECT count(*) FILTER (WHERE verified) FROM corpus.rules),
                        (SELECT coalesce(md5(string_agg(
                             rule_id::text || '|' || coalesce("사업명",'') || '|' ||
                             coalesce(비목,'') || '|' || 허용 || '|' ||
                             사전승인::text || '|' ||
                             coalesce(array_to_string(
                                 (SELECT array_agg(x ORDER BY x)
                                    FROM unnest(사전승인_조건) AS x), '~'), '') || '|' ||
                             coalesce(한도_유형,'') || '|' || coalesce(한도_값::text,'') || '|' ||
                             coalesce(한도_단위,'') || '|' ||
                             coalesce(array_to_string(증빙,'~'),'') || '|' ||
                             coalesce(array_to_string(금지예시,'~'),'') || '|' ||
                             coalesce(array_to_string(허용예시,'~'),'') || '|' ||
                             근거::text || '|' || verified::text,
                             '#' ORDER BY rule_id)), 'empty')
                         FROM corpus.rules),
                        -- 우선순위 규칙·체크항목은 행이 적어 행 전체를 해시한다(컬럼 단위 수정도 잡는다).
                        (SELECT md5(coalesce(string_agg(t::text, '#' ORDER BY prec_id), ''))
                           FROM corpus.precedence_rules t),
                        (SELECT md5(coalesce(string_agg(t::text, '#' ORDER BY code), ''))
                           FROM corpus.check_items t),
                        -- item_alias 는 embedding 컬럼이 커서 행 수·최대 id·최근 생성 시각만 본다.
                        (SELECT count(*) || ':' || coalesce(max(alias_id), 0) || ':'
                                || coalesce(max(created_at)::text, '')
                           FROM corpus.item_alias),
                        -- staged 문서를 활성화하면 청크 수는 그대로이고 상태만 바뀐다
                        (SELECT count(*) FROM corpus.chunks WHERE status = 'active')"""


@ttl_캐시("SUDDOE_DATA_VERSION_TTL", 300, 구이름="SUDDOE_CONFIG_HASH_TTL")  # 구 이름 호환(제거 예정: 다음 릴리스)
def _데이터버전_해시_조회() -> str:
    """DB 를 못 읽으면 빈 문자열(캐시하지 않는다)."""
    행 = _질의(_데이터버전_해시_SQL)
    if not 행:
        return ""
    n = 행[0]
    h = hashlib.sha1("|".join(map(str, n)).encode()).hexdigest()[:10]
    return f"c{n[0]}-e{n[1]}-r{n[2]}-d{n[3]}-rule{n[5]}v{n[6]}-rc{str(n[7])[:8]}-{h}"


def _데이터버전_해시() -> str:
    return _데이터버전_해시_조회() or "unknown"
