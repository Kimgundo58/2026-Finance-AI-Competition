# -*- coding: utf-8 -*-
"""POST /api/questions — 판정 전에 사용자에게 물을 확인 질문을 규정 원문에서 만든다.

새 화면은 비목을 고르지 않는다: 확정비목 없이 오면 ③ 서버 분류(_분류)가 정해 응답 `분류`({비목, 세목, 표시문, 품목}) · `분류안내` 로 알리고,
화면은 그 비목 · 세목을 /api/judge 의 확정비목 · 확정세목 으로 보낸다(판정 결과 `분류.표시문` 이 결과 화면 분류 줄, 저장은 persist.판정_저장).
question_generate.질문생성() 을 부르고 질문_공개() 로 화면 값만 내보낸다. 충족답·할일·근거조문은 서버에 남긴다.
응답의 질문세트는 응답 캐시 키다. 화면이 이 값을 /api/judge 로 보내면 판정이 저장해 둔 질문으로 답을 맞춘다(24시간 보관).
어떤 실패도 오류로 보이지 않는다. DB·LLM 오류와 요청 한도는 200 과 상태 '실패' 로 답하고 화면은 바로 결과로 넘어간다.
비목이 10종 밖이거나 등록되지 않은 사업명만 422 다. 설계: docs/LLM_판정아키텍쳐/05_API.md.
로그에는 상태·질문 수·버린 사유·지연만 남긴다. 사용자 입력과 질문 문장은 남기지 않는다.
"""
from __future__ import annotations

import logging
import time
import concurrent.futures
import contextvars
import json
import queue

from fastapi import APIRouter, HTTPException, Request

from server import auth, mock_flow, response_cache, research_sessions, ontology_readiness
from server._common import MOCK, 답변_반영, 비목_ENUM, 클라이언트_ip, _sse, _sse응답, _워커_대기, 온톨로지_사업
from server.catalog import _비목_표준화, _사업명_표준화
from server.models import 질문요청
from server.rate_limit import 제한

_log = logging.getLogger("uvicorn.error")

router = APIRouter()

# 질문 화면을 띄워 놓고 오래 있어도 답변이 사라지지 않게 일반 응답 캐시보다 길게 둔다.
_질문세트_TTL초 = 24 * 3600

_실패_응답 = {"상태": "실패", "질문세트": None, "질문": [], "추가입력": [], "용어풀이": []}
# LLM 이 실패해 코드 항목(추가 입력 · 부가세 질문)만 남은 결과. 일시적 장애를 오래 고정하지 않는다.
_임시_TTL초 = 1800


def _로그(상태: str, 질문수: int, 버린사유: list[str], 캐시: bool, t0: float) -> None:
    _log.info("확인 질문 상태=%s 질문수=%d 버린사유=%s 캐시=%s 지연ms=%d",
              상태, 질문수, 버린사유, 캐시, int((time.time() - t0) * 1000))


@router.post("/api/questions")
def questions(req: Request, body: 질문요청):
    # 조사 계약을 요청하지 않은 기존 JSON 클라이언트는 원래 응답 모양을 유지한다.
    # 새 화면은 SSE 또는 조사ID로 흐름을 시작한다(재점검은 SSE로 새 ID 발급).
    # 비목 없이 온 요청은 JSON 이어도 조사 세션 경로로 보내 ③ 서버 분류(응답 분류 · 분류안내)를 받게 한다(v045)
    if (body.확정비목 and not body.조사ID and not body.웹조사 and body.조사답변 is None
            and 'text/event-stream' not in req.headers.get('accept', '')):
        return _기존_질문(req, body)
    org, account = auth.검증된_org(req), auth.검증된_account(req)
    조사ID = body.조사ID or research_sessions.생성(org, account)
    세션 = research_sessions.조회(조사ID, org, account)
    if 세션 is None:
        raise HTTPException(400, '조사 세션이 만료되었거나 유효하지 않습니다')
    if not 세션.lock.acquire(blocking=False):
        raise HTTPException(409, '조사가 진행 중입니다')
    try:
        if body.확정비목:                     # 비우면 ③ 에서 서버가 정한다(재설계 11-11)
            body.확정비목 = _비목_표준화(body.확정비목)
            if body.확정비목 not in 비목_ENUM:
                raise HTTPException(422, f"비목은 다음 10종 중 하나여야 합니다: {비목_ENUM}")
        body.사업명 = _사업명_표준화(body.사업명)
        if not MOCK:
            try:
                ontology_readiness.확인(body.사업명)
            except Exception as e:  # noqa: BLE001
                raise HTTPException(503, '선택한 사업의 규정을 확인할 수 없습니다') from e
        입력 = research_sessions.입력키(body.사업명, body.확정비목, body.확정세목,
                                     body.품목, body.금액, body.용도, body.부가세구분, body.질문원문)
        규정버전, 기관버전 = response_cache.요청_해시(), response_cache.l3_해시(org)
        if not MOCK and (규정버전 in ('unknown', '', None) or (org and not 기관버전)):
            raise HTTPException(503, '규정 버전을 확인할 수 없습니다')
        버전 = json.dumps([규정버전, 기관버전, 온톨로지_사업(body.사업명)], ensure_ascii=False)
        요청키 = json.dumps([입력, body.질문원문, body.웹조사, body.공개검색어, body.판매URL, body.거래처], ensure_ascii=False)
        if 세션.입력 is not None and (세션.입력 != 입력 or 세션.버전 != 버전 or getattr(세션, '요청키', None) != 요청키):
            raise HTTPException(409, '입력이 변경되어 새 조사가 필요합니다')
    except BaseException:
        세션.lock.release()
        raise
    세션.lock.release()

    import research_runtime
    요청예산 = 세션.budget.request()            # 세션 예산을 나눠 쓰고 취소는 이 요청에만 건다(리뷰 28-6)

    def 작업(emit):
        if not 세션.lock.acquire(blocking=False):
            raise HTTPException(409, '조사가 진행 중입니다')
        try:
            if 세션.입력 is not None and (세션.입력 != 입력 or 세션.버전 != 버전 or getattr(세션, '요청키', None) != 요청키):
                raise HTTPException(409, '입력이 변경되어 새 조사가 필요합니다')
            with research_runtime.activate(요청예산, emit=emit):
                if body.웹조사 and body.조사답변 is None:
                    if not 세션.조사완료:
                        try:
                            # mock: 외부 호출 없이 고정 조사 결과(스펙 8-5). 검색어 없이도 돈다
                            if MOCK:
                                세션.조사 = mock_flow.품목조사(body.품목, body.거래처)
                            # v046: 공개 검색어 없이 늘 조사한다. 계획 LLM 이 지출 항목 · 사용 목적 낱말로 검색어를 만들고,
                            # 판매URL 은 먼저 읽을 주소, 거래처는 판매자 · 업체 이용 조건 조사 목적이다(web_research.research)
                            else:
                                # ② 품목 조사(재설계 11-11): 비목을 정하기 전이라 규범 요건 없이 지출 문맥만 준다
                                준비 = {'요건': [], '문맥': {k: v for k, v in (('품목', body.품목), ('용도', body.용도),
                                                                             ('금액', body.금액)) if v not in (None, '')}}
                                if not MOCK:
                                    ok, 사유 = 제한.통과(클라이언트_ip(req))
                                    if not ok:
                                        raise RuntimeError(사유)
                                if not MOCK and body.확정비목:      # 옛 화면: 비목을 먼저 고른 경우만 규범 요건을 조사 자료로
                                    import ontology_research_plan
                                    준비 = ontology_research_plan.조사_준비(
                                        {'사업명': body.사업명, '비목': body.확정비목,
                                         '확정세목': body.확정세목, '품목': body.품목,
                                         '용도': body.용도, '질문': body.질문원문},
                                        org_id=org, account_id=account)
                                import web_research
                                세션.조사 = web_research.research(None,
                                    requirements=준비['요건'], context=준비['문맥'],
                                    public_url=body.판매URL, budget=요청예산, emit=emit, vendor=body.거래처)
                        except Exception as e:
                            _log.warning('공개 정보 조사 실패: %s', type(e).__name__)
                            세션.조사 = {'상태': '실패', '후보': [], '출처': []}
                            research_runtime.emit('공개 조사', research_runtime.공개조사_실패문장, 상태='실패')
                        세션.조사완료 = True
                        세션.입력, 세션.버전, 세션.요청키 = 입력, 버전, 요청키
                    return {'상태': '조사확인', '질문세트': None, '질문': [], '추가입력': [],
                            '용어풀이': [], '조사ID': 조사ID, '조사': 세션.조사}
                if body.웹조사 and not 세션.조사완료:
                    raise HTTPException(400, '확인할 조사 결과가 없습니다')
                try:
                    조사확인 = research_sessions.후보_답변(세션, [a.model_dump() for a in (body.조사답변 or [])])
                except ValueError as e:
                    raise HTTPException(400, '조사 답변을 확인해 주세요') from e
                # ③-R 재조사 답은 세션.재조사 후보에만 붙인다(스펙 8-2 ③)
                try:
                    재조사확인 = research_sessions.재조사_답변(세션, [a.model_dump() for a in (body.재조사답변 or [])])
                except ValueError as e:
                    raise HTTPException(400, '조사 답변을 확인해 주세요') from e
                # 재조사 답 · [직접 입력하기] 품목 설명은 한 번 받으면 세션에 남긴다(스펙 8-2 ③). 다음 요청이 빼고 보내도
                # 세션 값을 쓰고, 같은 값을 다시 보내도 된다. 확인 결과에 넣어 분류 · 질문 · 판정 프롬프트까지 가고 답키에도 들어가
                # 바뀌면 ③ 분류를 다시 한다. 판정(답변_검증)은 세션 값으로 같은 키를 다시 만든다
                # 재조사 후보를 보여 줬는데 답 없이 새 품목설명만 오면 다음 단계로 넘기지 않는다(조율 결정 v048: 조사답변 과 같은 규칙 ·
                # 같은 400). 찾은 후보가 0개면 검사하지 않는다. 같은 설명을 다시 보낸 요청(다시 보내기)은 저장된 결과를 받는다
                새설명 = body.품목설명.strip() if isinstance(body.품목설명, str) else ''
                if not 재조사확인 and research_sessions.재조사_답_대기(세션) and 새설명 and 새설명 != 세션.품목설명:
                    raise HTTPException(400, '조사 답변을 확인해 주세요')
                if 재조사확인:
                    세션.재조사확인 = 재조사확인
                if isinstance(body.품목설명, str) and body.품목설명.strip():
                    세션.품목설명 = body.품목설명.strip()
                조사확인 = research_sessions.재조사_붙이기(
                    research_sessions.설명_붙이기(조사확인, 세션.품목설명), 세션.재조사확인)
                답키 = research_sessions.답변키(조사확인)
                if 세션.질문결과 is not None and 세션.확인답키 == 답키 and not 세션.분류실패:
                    if emit:
                        emit({'id': '저장결과', '단계': '확인질문', '상태': '완료', '문장': '저장된 확인 질문을 불러왔어요.'})
                    return 세션.질문결과
                분류결과 = None
                if not body.확정비목:
                    # 대기재조사: 재조사 후보를 보여 주고 아직 답을 받지 않았다. 분류가 없으면 설명입력 · 판정 대신 이 후보로 재조사확인 을
                    # 다시 낸다(위 400 규칙과 어긋나지 않게, v048 검토). 읽은주소: ② 가 읽은 주소(세션.조사 출처)를 재조사가 다시 읽지 않게 넘긴다
                    분류결과 = _분류(body, 조사확인, 조사ID, 재조사함=세션.재조사 is not None, 설명캐시=세션.설명정리,
                                   ip=클라이언트_ip(req),
                                   대기재조사=세션.재조사 if research_sessions.재조사_답_대기(세션) else None,
                                   읽은주소=_출처_주소(세션.조사))
                    세션.분류, 세션.노드결정 = 분류결과['분류'], 분류결과['노드결정']
                    # 재조사는 세션마다 한 번(스펙 8-2 서버 순서). 못 찾음 · 시간 부족({후보: []})도 「했음」 으로 남겨 다시 하지 않는다
                    if isinstance(분류결과.get('재조사'), dict) and 세션.재조사 is None:
                        세션.재조사 = 분류결과['재조사']
                    if not 분류결과['분류']:
                        # 다음행동(재조사확인 | 설명입력 | 다시시도 | 판정)과 재조사 후보는 화면이 다음 단계를 고르는 값이다(스펙 8-2 ②).
                        # 실제 경로는 _분류없음_다음, mock 은 mock_flow.분류 가 같은 모양으로 낸다. 재조사 후보는 재조사확인 일 때만 낸다
                        결과 = {**_실패_응답, '상태': '분류없음', '조사ID': 조사ID, '조사': 세션.조사,
                              '분류': None, '분류안내': 분류결과['안내'],
                              **({'다음행동': 분류결과['다음행동']} if 분류결과.get('다음행동') else {}),
                              **({'재조사': 분류결과['재조사']} if 분류결과.get('다음행동') == '재조사확인' else {})}
                        # 장애로 분류하지 못한 결과(분류실패)는 같은 답으로 다시 불러도 내주지 않고 분류를 다시 한다.
                        # 판정 요청의 답변 검증(research_sessions.답변_검증)은 맞는 분류가 없을 때처럼 이 결과로 한다
                        세션.입력, 세션.버전, 세션.요청키, 세션.확인답키, 세션.질문결과 = 입력, 버전, 요청키, 답키, 결과
                        세션.분류실패 = bool(분류결과.get('실패'))
                        세션.질문세트, 세션.판정결과 = None, None
                        return 결과
                    body.확정비목, body.확정세목 = 분류결과['분류']['비목'], 분류결과['분류']['세목']
                if MOCK:                        # ④-2 판정용 조사 고정 진행 줄(외부 호출 없음, 스펙 8-5)
                    mock_flow.판정조사(body.확정비목)
                # ④-2 판정용 조사(재설계 11-13, v046 부터 항상): 웹조사 값과 상관없이 분류가 있으면 돈다(별칭 일치 · 재점검 확정비목 포함).
                # 질문 생성이 ③ · ④ 뒤 ⑤ 앞에서 부르고, 남은 시간이 모자라면 건너뛴다(question_generate.판정용_조사)
                판정조사설정 = _판정조사_설정(body)
                결과 = _기존_질문(req, body, 조사확인 if body.웹조사 or 조사확인.get('품목설명') else None, 답키, 준비확인=True,
                              노드결정=(분류결과 or {}).get('노드결정'), 판정조사설정=판정조사설정)
                세션.판정조사 = 결과.get('_판정조사') if isinstance(결과, dict) else None
                결과 = {**{k: v for k, v in 결과.items() if k != '_판정조사'}, '조사ID': 조사ID, '조사': 세션.조사}
                if 분류결과:
                    결과 = {**결과, '분류': 분류결과['분류'], '분류안내': 분류결과['안내']}
                세션.입력, 세션.버전, 세션.질문세트 = 입력, 버전, 결과.get('질문세트')
                세션.요청키, 세션.확인답키, 세션.질문결과 = 요청키, 답키, 결과
                세션.판정결과, 세션.분류실패 = None, False
                return 결과
        finally:
            세션.lock.release()

    if 'text/event-stream' not in req.headers.get('accept', ''):
        return 작업(None)

    def gen():
        이벤트 = queue.Queue()
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = ex.submit(contextvars.copy_context().run, 작업, 이벤트.put)
        끝남 = False
        try:
            yield from _워커_대기(future, 이벤트, 12)
            try:
                결과 = future.result()
            except Exception as e:
                _log.warning('조사 실패: %s', type(e).__name__)
                결과 = {**_실패_응답, '조사ID': 조사ID,
                      '조사': {'상태': '실패', '후보': [], '출처': []}}
            끝남 = True
            yield _sse('완료', 결과)
        finally:
            if not 끝남:
                요청예산.cancel()
            ex.shutdown(wait=False, cancel_futures=True)
    return _sse응답(gen())


def _판정조사_설정(body: 질문요청) -> dict | None:
    """④-2 판정용 조사 설정 {판매URL, 거래처}. 공개 검색어는 넘기지 않는다(v046: 지출 항목 · 사용 목적 낱말로 검색).
    MOCK 은 mock_flow.판정조사 가 고정 진행 줄을 내므로 None."""
    return None if MOCK else {'판매URL': body.판매URL, '거래처': body.거래처}


# 분류를 하지 못한 장애(DB · LLM 실패). 규정에 맞는 분류가 없다는 안내(item_classify.안내문(None))와 구분한다
_분류실패_안내 = "지금은 분류를 확인하지 못했어요. 잠시 후 다시 시도해 주세요."
# 분류없음 의 다음행동별 안내(설명입력 은 item_classify.안내문(None), 다시시도 는 _분류실패_안내). mock_flow.분류 도 이 문장을 쓴다
재조사확인_안내 = "규정에서 맞는 분류를 찾지 못해 품목을 더 알아봤어요. 찾은 내용을 확인해 주세요."
분류없이판정_안내 = "규정에서 맞는 분류를 찾지 못했어요. 분류 없이 점검하고 집행 전에 확인할 곳을 알려 드릴게요."

# ③-R 재조사 시간(스펙 8-2 시간 예산): ④-2 판정용 조사 몫(question_generate.판정조사_예비초 100 + 판정조사_최소초 20)을
# 남길 수 있을 때만, 최대 60초. 남는 시간이 15초보다 짧으면 검색 한 번도 끝내기 어려워 재조사 없이 설명입력 으로 간다.
# 재조사와 ④-2 사이에는 다시 분류(분류 LLM 한 번, item_classify.분류_제한초() = SUDDOE_CLASSIFY_TIMEOUT 기본 40초)가 있어
# 그 시간도 더 남긴다(v048 검토: 120초만 남기면 재조사가 제한초를 다 쓴 세션은 ④-2 가 늘 건너뛰어졌다).
# [직접 입력하기] 정정이 있을 때 도는 설명 정리 LLM(같은 제한 시간)은 남기지 않는다: 둘 다 남기면 재조사 시작에 215초가 필요해
# ② 품목 조사 뒤에는 재조사가 거의 돌지 못한다
재조사_남길초 = 120
재조사_최대초 = 60
재조사_최소초 = 15
재조사_시작문장 = "품목을 더 알아보고 있어요."
재조사_찾음문장 = "품목 정보를 더 찾았어요."
재조사_없음문장 = "더 찾은 품목 정보는 없어요."


def _출처_주소(조사: dict | None) -> list[str]:
    """② 품목 조사가 읽은 주소(세션.조사 출처). 실제 조사는 주소 글, mock 은 {url} 이라 둘 다 받는다."""
    out = []
    for x in (조사 or {}).get('출처') or []:
        u = x if isinstance(x, str) else x.get('url') if isinstance(x, dict) else None
        if isinstance(u, str) and u and u not in out:
            out.append(u)
    return out


def _분류(body: 질문요청, 조사확인: dict | None, 조사ID: str | None = None, 재조사함: bool = False,
         설명캐시: dict | None = None, ip: str | None = None, 대기재조사: dict | None = None,
         읽은주소: list[str] | None = None) -> dict:
    """③ 품목 → 세목 → 비목(재설계 11-11). {분류 | None, 노드결정 | None, 안내, 실패?, 다음행동?, 재조사?}. 화면에는 분류 이름과 안내만 나간다.

    분류 = {비목, 세목, 표시문, 품목}. 맞는 분류가 없으면 분류 None 이고 _분류없음_다음 이 ③-R 재조사(세션에서 한 번)와
    다음행동(재조사확인 | 설명입력 | 판정)을 정한다(스펙 5-1 · 8-2).
    장애(DB 예외 · 분류 LLM 실패)도 분류 None 이지만 실패=True · 다음행동 다시시도 · _분류실패_안내 로 구분하고 재조사하지 않는다
    (호출자는 이 결과를 같은 답에 다시 내주지 않는다). 디버그(버린 후보 · 다른 경로 · LLM 이유)는 로그에 개수만 남긴다.
    재조사함 = 조사 세션에 재조사 결과가 있다(세션.재조사). 조사ID 는 mock 가지가 읽는다. ip 는 재조사의 요청 한도 확인용.
    대기재조사 = 재조사 후보를 보여 줬는데 아직 답이 없는 세션.재조사(없으면 None). 분류가 없으면 이 후보로 재조사확인 을 다시 낸다.
    읽은주소 = ② 품목 조사가 읽은 주소. 재조사가 같은 페이지를 다시 읽지 않는다.
    설명캐시(세션.설명정리): 사용자가 쓴 한 문장을 분류 자료 JSON 으로 바꾼 결과(5-2, item_classify.설명_반영). MOCK 은 바꾸지 않는다.
    조사확인 은 바꾸지 않는다: 질문 · 판정 프롬프트의 사용자 진술은 쓴 문장 그대로다(8-4).
    """
    if MOCK:                    # 외부 호출 없는 고정 분류(스펙 8-5). 낱말별 가지는 mock_flow.분류
        return mock_flow.분류(body, 조사확인, 조사ID, 재조사함, 대기재조사)
    import item_classify
    import ontology_lookup
    from _lib import db
    try:
        with db.connect(connect_timeout=5, autocommit=True) as c:
            cur = c.cursor()
            레이블 = [x for x in ontology_lookup.레이블_목록(cur, body.사업명) if x["비목"] in 비목_ENUM]
            r = item_classify.분류(cur, body.사업명, {"품목": body.품목, "용도": body.용도, "금액": body.금액, "거래처": body.거래처},
                                  레이블, item_classify.확인정보_글(조사확인, (조사확인 or {}).get('품목설명') or body.품목설명),
                                  설명캐시=설명캐시)
    except Exception as e:                                    # noqa: BLE001
        _log.warning("분류 실패: %s", type(e).__name__)
        return {"분류": None, "노드결정": None, "안내": _분류실패_안내, "실패": True, "다음행동": "다시시도"}
    d = r["디버그"]
    _log.info("분류 방법=%s 후보=%s 버림=%s 다른경로=%s 설명정리=%s ms=%s", r["방법"], d.get("후보수"), len(d.get("버린후보") or []),
              len(d.get("다른경로") or []), d.get("설명정리"), d.get("ms"))
    if not r["분류"] and d.get("LLM실패"):                    # 분류 LLM 이 실패했다. 맞는 분류가 없다는 뜻이 아니다
        return {"분류": None, "노드결정": None, "안내": _분류실패_안내, "실패": True, "다음행동": "다시시도", "_디버그": d}
    if not r["분류"]:
        return {"분류": None, "노드결정": None, "_디버그": d,
                **_분류없음_다음(body, 조사확인, 재조사함, r.get("필요정보") or [], ip, 대기재조사, 읽은주소)}
    return {"분류": r["분류"], "노드결정": r["노드결정"], "안내": item_classify.안내문(r["분류"]), "_디버그": d}


def _분류없음_다음(body: 질문요청, 조사확인: dict | None, 재조사함: bool, 필요정보: list[str], ip: str | None = None,
               대기재조사: dict | None = None, 읽은주소: list[str] | None = None) -> dict:
    """맞는 분류가 없을 때(장애 아님)의 서버 순서(스펙 8-2): 재조사를 아직 안 했으면 ③-R 재조사(한 번) →
    찾음: 재조사확인 / 못 찾음 · 검색 실패 · 시간 부족: 설명을 받은 적이 없으면 설명입력, 있으면 판정.
    재조사 뒤 다시 분류해도 없으면(재조사함) 같은 규칙으로 설명입력 | 판정. 반환 {안내, 다음행동, 재조사?}.
    재조사를 시도했으면(찾지 못했어도) 재조사 {후보: [...]} 를 실어 호출자가 세션에 「했음」 으로 남긴다.
    재조사 후보에 아직 답하지 않았으면(대기재조사) 재조사를 다시 돌리지 않고 그 후보로 재조사확인 을 다시 낸다: 설명입력 을 내면
    그 품목설명을 400 으로 거절하게 된다(questions() 의 재조사_답_대기 규칙, v048 검토).
    필요정보가 비면 재조사를 돌리지 않는다(② 와 같은 조사가 된다). 못 찾음과 같이 「했음」 으로 남기고 설명입력 | 판정 으로 간다."""
    import item_classify
    if 대기재조사 and 대기재조사.get("후보"):
        return {"안내": 재조사확인_안내, "다음행동": "재조사확인", "재조사": 대기재조사}
    재조사 = None
    if not 재조사함:
        재조사 = _재조사(body, 필요정보, ip, 읽은주소) if 필요정보 else {"후보": []}
        if 재조사["후보"]:
            return {"안내": 재조사확인_안내, "다음행동": "재조사확인", "재조사": 재조사}
    다음 = ({"안내": 분류없이판정_안내, "다음행동": "판정"} if research_sessions.설명_받음(조사확인)
          else {"안내": item_classify.안내문(None), "다음행동": "설명입력"})
    return {**다음, **({"재조사": 재조사} if 재조사 is not None else {})}


def _재조사(body: 질문요청, 필요정보: list[str], ip: str | None = None, 읽은주소: list[str] | None = None) -> dict:
    """③-R 분류용 재조사(스펙 5-1). ② 와 같은 web_research.research(계획 LLM · 후보 LLM · 코드 검사)를 조사 목적만 필요정보로 바꿔
    한 번 돈다. 반환 {후보: [② 후보와 같은 모양]}. 후보 [] = 못 찾음 · 검색 실패 · 시간 부족 · 요청 한도. 예외를 올리지 않는다.
    시간: 세션 예산에서 ④-2 몫(재조사_남길초) + 다시 분류할 시간(item_classify.분류_제한초())을 남기고 최대 재조사_최대초.
    결과는 세션.조사 와 따로 세션.재조사 에 둔다(호출자). 읽은주소(② 가 읽은 페이지)는 다시 읽지 않는다."""
    import item_classify
    import research_runtime
    import uuid
    예산 = research_runtime.current_budget()
    제한초 = min(재조사_최대초,
              (예산.remaining_seconds() if 예산 is not None else 0) - 재조사_남길초 - item_classify.분류_제한초())
    if 예산 is None or 제한초 < 재조사_최소초:
        _log.info("재조사 건너뜀: 시간 부족")
        return {"후보": []}
    if ip is not None:
        ok, _사유 = 제한.통과(ip)
        if not ok:
            _log.info("재조사 건너뜀: 요청 한도")
            return {"후보": []}
    문맥 = {**{k: v for k, v in (('품목', body.품목), ('용도', body.용도), ('금액', body.금액)) if v not in (None, '')},
          **({'필요정보': list(필요정보)} if 필요정보 else {})}
    event_id = uuid.uuid4().hex
    research_runtime.emit("재조사", 재조사_시작문장, id=event_id)

    def 알림(e: dict) -> None:
        e = dict(e)
        research_runtime.emit(e.pop("단계", "재조사"), e.pop("문장", ""), e.pop("상태", "진행중"), **e)
    try:
        import web_research
        r = web_research.research(None, requirements=[], context=문맥, public_url=body.판매URL, vendor=body.거래처,
                                  budget=예산, emit=알림, 제한초=제한초, 읽은주소=읽은주소)
    except Exception as e:                                    # noqa: BLE001
        _log.warning("재조사 실패: %s", type(e).__name__)
        r = {}
    후보 = [c for c in (r.get("후보") if isinstance(r, dict) else None) or [] if isinstance(c, dict) and c.get("id")]
    research_runtime.emit("재조사", 재조사_찾음문장 if 후보 else 재조사_없음문장, "완료", id=event_id)
    return {"후보": 후보}


def _기존_질문(req: Request, body: 질문요청, 조사확인=None, 답키=None, 준비확인=False, 노드결정=None,
              판정조사설정=None) -> dict:
    """응답: {상태: 있음|없음|바로판정|실패, 질문세트, 질문: [{id, 질문문, 이유, 선택지, 근거}], 추가입력, 용어풀이}."""
    t0 = time.time()
    body.확정비목 = _비목_표준화(body.확정비목)
    if body.확정비목 not in 비목_ENUM:
        raise HTTPException(422, f"비목은 다음 10종 중 하나여야 합니다: {비목_ENUM}")
    try:
        body.사업명 = _사업명_표준화(body.사업명)
    except HTTPException as e:
        if e.status_code < 500:
            raise
        _로그("실패", 0, ["사업목록"], False, t0)      # 사업 목록을 못 읽었다(DB 장애)
        return dict(_실패_응답)
    if not MOCK and not 준비확인:
        try:
            ontology_readiness.확인(body.사업명)
        except Exception:  # noqa: BLE001
            _로그("실패", 0, ["규정확인"], False, t0)
            return dict(_실패_응답)
    if MOCK:
        return mock_flow.질문(body.품목)
    if not 답변_반영():                                    # 답을 쓰지 않을 때는 묻지 않는다
        return {"상태": "없음", "질문세트": None, "질문": [], "추가입력": [], "용어풀이": []}

    org, account = auth.검증된_org(req), auth.검증된_account(req)
    try:
        return _live_질문(body, org, account, 클라이언트_ip(req), t0, 조사확인, 답키, 노드결정, 판정조사설정)
    except Exception as e:                                    # noqa: BLE001
        _log.warning("확인 질문 실패: %s", type(e).__name__)
        _로그("실패", 0, ["예외"], False, t0)
        return dict(_실패_응답)


def 질문세트_키(org: str | None, account: str | None, 사업명, 확정비목, 확정세목, 부가세구분, 세부유형, 품목, 금액, 용도,
             질문원문) -> str:
    """질문세트(캐시 키). 캐시 행은 계정 단위라(RLS) 키에도 org·계정을 넣는다. L3 를 새로 올리면 키가 바뀐다.

    판정 라우트가 질문세트 없이 온 요청(질문 0개)에서 같은 키를 다시 만들어 노드 결정만 읽는다(routes_judge._노드결정_읽기).
    """
    return response_cache.캐시키("questions", org, account, 사업명, 온톨로지_사업(사업명), 확정비목, 확정세목, 부가세구분,
                              세부유형, 품목, 금액, 용도, 질문원문, response_cache.l3_해시(org))


def 질문경로_일치(저장값, 사업명):
    """옛 질문세트를 새 판정에 섞지 않는다. 표식 없는 과거 값은 미설정 사업에서만 허용한다."""
    if not isinstance(저장값, dict):
        return False
    현재 = 온톨로지_사업(사업명)
    기록 = 저장값.get('_온톨로지경로')
    return (not 현재) if 기록 is None else type(기록) is bool and 기록 == 현재


def _live_질문(body: 질문요청, org: str | None, account: str | None, ip: str, t0: float, 조사확인=None, 답키=None,
             노드결정=None, 판정조사설정=None) -> dict:
    """`판정조사설정`({판매URL, 거래처})이 있으면 질문 생성이 ④-2 판정용 조사를 하고, 결과를 저장값 「판정조사」 와
    응답의 서버 전용 키 `_판정조사` 로 낸다(호출자 작업() 이 세션에 옮기고 화면 응답에서 뺀다).
    조사 확인 없이 온 요청(별칭 일치 · 재점검)은 판정조사가 거래처 · 판매URL 에 따라 달라지므로 그 둘을 키에 더한다."""
    import question_generate

    k = 질문세트_키(org, account, body.사업명, body.확정비목, body.확정세목, body.부가세구분,
                body.세부유형, body.품목, body.금액, body.용도, body.질문원문)
    if 조사확인 is not None:
        k = response_cache.캐시키('questions_research', org, account, k, body.조사ID, 답키)
    elif 판정조사설정:
        k = response_cache.캐시키('questions_judge_research', org, account, k,
                               판정조사설정.get('거래처'), 판정조사설정.get('판매URL'))

    def 응답(저장값: dict) -> dict:
        return {"상태": 저장값["상태"], "질문세트": k,
                "질문": [question_generate.질문_공개(q) for q in 저장값["질문"]],
                "추가입력": 저장값.get("추가입력") or [], "용어풀이": 저장값.get("용어풀이") or [],
                **({"_판정조사": 저장값["판정조사"]} if 저장값.get("판정조사") else {})}

    # 조회() 를 쓰지 않는다. 아직 카운트를 올리지 않았으므로 적중해도 되돌릴 것이 없다.
    저장값 = response_cache.질문세트_조회(k, org, account)
    if 질문경로_일치(저장값, body.사업명):
        _로그(저장값["상태"], len(저장값["질문"]), [], True, t0)
        return 응답(저장값)

    ok, _사유 = 제한.통과(ip)
    if not ok:
        _로그("실패", 0, ["요청한도"], False, t0)      # 판정 요청이 자기 한도 오류를 낸다
        return dict(_실패_응답)

    데이터버전해시 = response_cache.요청_해시()
    r = question_generate.질문생성(
        {"사업명": body.사업명, "비목": body.확정비목, "세부유형": body.확정세목 or body.세부유형, "품목": body.품목,
         "용도": body.용도, "금액": body.금액, "질문": body.질문원문,
         "확정세목": body.확정세목, "부가세구분": body.부가세구분,
         **({'_조사확인': 조사확인} if 조사확인 is not None else {}),
         **({'_판정조사설정': 판정조사설정} if 판정조사설정 else {}),
         **({'_노드결정': 노드결정} if 노드결정 else {})},
        org_id=org, account_id=account)
    메타 = r.get("메타") or {}
    if not 메타.get("LLM호출"):                         # LLM 을 부르지 않았으면 일일 카운트를 되돌린다
        제한.카운트_복원()
    # 버린질문의 키는 질문 문장이라 사유 코드만 남긴다
    버린사유 = sorted({사유 for 항목 in (메타.get("버린질문") or {}).values() for 사유 in 항목["사유"]})
    _로그(r["상태"], len(r["질문"]), 버린사유 or ([메타["사유"]] if 메타.get("사유") else []), False, t0)
    if r["상태"] == "실패":                             # 일시적 장애를 24시간 고정하지 않는다
        # 질문 LLM 이 실패해도 ④-2 판정용 조사 결과는 판정에 넘긴다(세션에만, 화면 응답에서는 작업() 이 뺀다)
        return {**_실패_응답, **({"_판정조사": r["판정조사"]} if r.get("판정조사") else {})}
    # 노드결정(5-13) · 상황노드(5-14 C)는 서버에만 둔다. 응답() 은 내보내지 않고 판정 라우트가 읽는다
    저장값 = {"상태": r["상태"], "질문": r["질문"], "추가입력": r.get("추가입력") or [], "용어풀이": r.get("용어풀이") or [],
             "_온톨로지경로": 온톨로지_사업(body.사업명),
             **({"노드결정": r["노드결정"]} if r.get("노드결정") else {}), **({"상황노드": r["상황노드"]} if r.get("상황노드") else {}),
             **({"L3숫자라벨": r["L3숫자라벨"]} if r.get("L3숫자라벨") else {}),
             **({"판정조사": r["판정조사"]} if r.get("판정조사") else {})}
    저장됨 = response_cache.저장(k, 저장값, 종류="questions", org=org, account=account, 데이터버전_해시=데이터버전해시,
                              ttl초=_임시_TTL초 if 메타.get("사유") == "LLM실패" else _질문세트_TTL초)
    if not 저장됨 and 저장값["질문"]:                    # 답을 받아도 판정에서 질문을 찾지 못하니 묻지 않는다
        _로그("실패", 0, ["저장실패"], False, t0)
        return dict(_실패_응답)
    # 다시 점검처럼 질문 키가 안 맞는 판정이 읽는 보조 키. 노드결정만 둔다: 품목에서 고르니 키(품목)와 맞는다.
    # 상황노드는 질문 글에서 고르는데 보조 키에 질문 글이 없어 같은 품목의 다른 계획이 덮어쓰고, 다시 점검 판정은 질문 글 없이 돌아 상황을 확인할 수 없다.
    # L3숫자라벨(5-15 ⑤)은 기관 조문과 분류 노드에서 고르지 질문 글에서 고르지 않아 보조 키에도 둔다(판정이 지금 조문과 다시 맞춰 보고 안 맞으면 버린다)
    if 조사확인 is None and 저장됨 and (저장값.get("노드결정") or 저장값.get("L3숫자라벨")):
        response_cache.저장(노드결정_키(org, account, body.사업명, body.확정비목, body.확정세목, body.품목),
                          {k: 저장값[k] for k in ("노드결정", "L3숫자라벨", "_온톨로지경로") if k in 저장값}, 종류="questions", org=org, account=account,
                          데이터버전_해시=데이터버전해시, ttl초=_질문세트_TTL초)
    return 응답(저장값)


def 노드결정_키(org: str | None, account: str | None, 사업명, 확정비목, 확정세목, 품목) -> str:
    """품목 노드 결정(5-13)만 담는 보조 키. 질문세트 키와 달리 금액 · 용도 · 질문원문 · 세부유형 · 부가세구분을 넣지 않는다.

    화면의 다시 점검 경로는 질문원문 없이 판정을 불러 질문세트 키가 안 맞는다(9d). 같은 품목 글이면 같은 노드라
    이 키로 결정만 읽는다. 노드 확인 답은 없으므로 「유사」 는 유사 그대로다. ID 는 판정이 다시 검사한다(ontology_lookup.노드결정_적용).
    """
    return response_cache.캐시키("node", org, account, 사업명, 온톨로지_사업(사업명), 확정비목, 확정세목, (품목 or "").strip())
