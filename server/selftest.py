# -*- coding: utf-8 -*-
"""mock 모드 고정 응답(목_정규화·목_판정)과 서버를 띄우지 않는 API 계약 자가검사.

routes_normalize·routes_judge 가 SUDDOE_MOCK=1 일 때 이 고정 응답을 내보낸다(프론트가 DB·LLM 없이 네 판정 화면을 그린다).
자가검사는 FastAPI TestClient 로 엔드포인트·SSE 이벤트·입력 검증·요청 제한을 확인하고 PASS/FAIL 을 출력한다.
판정 파이프라인은 가짜 orchestrate 로 바꿔 끼워 live 경로의 키 매핑만 확인한다(LLM·임베딩 모델을 부르지 않는다).
실행: SUDDOE_MOCK=1 python -m server.selftest   (python server/main.py --selftest 도 같다)
"""
from __future__ import annotations

import json
import os
import sys
import types

# mock 정규화 응답. 심층질문은 check_items 를 읽지 않으므로 빈 목록으로 live 응답과 키만 맞춘다.
목_정규화 = {
    "품목": "맥북", "금액": 2_500_000, "금액_추정여부": False,
    "용도": "디자이너 작업용",
    "비목후보": [{"비목": "기계장치", "신뢰도": 0.82}, {"비목": "재료비", "신뢰도": 0.31}],
    "결제수단": None, "구매명의": None, "신청일": None, "비교견적": None, "하위항목": None,
    "심층질문": [],
}

# mock 판정 응답 네 가지. 프론트는 ?목=조건부|불가|판단불가 로 바꿔 네 화면을 그린다.
# 해야할일의 code 는 corpus.check_items 에 실제로 있는 값만 쓰고, 없으면 code=None 에 구분을 붙이지 않는다.
# SSE 의 해야할일은 code(영문 키), 저장되는 할일(models.할일)은 코드(DB 컬럼)다.
목_판정: dict[str, dict] = {
    "가능": {
        "판정": "가능",
        "요약": "1인 1대 한도 내에서 구매 가능합니다.",
        "해야할일": [
            {"code": "비교견적준비", "구분": "결제전", "유형": "기타",
             "항목": "비교견적 2곳 이상 확보", "설명": "50만원 이상 구매는 비교견적을 남깁니다."},
            {"code": "사업비카드사용", "구분": "결제전", "유형": "기타",
             "항목": "사업비 카드로 결제", "설명": "현금·개인카드 결제는 인정되지 않습니다."},
            {"code": "자산등록", "구분": "결제후", "유형": "기타",
             "항목": "자산 등록", "설명": "취득일로부터 30일 이내에 자산으로 등록합니다."},
        ],
        "인용": [
            {"조번호": "제39조", "조제목": "기계장치",
             "원문": "기계장치는 사업계획서에 명시된 범위에서 취득할 수 있으며, 1인 1대를 원칙으로 한다.",
             "doc_id": "L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223"},
            {"조번호": "붙임2", "조제목": "비목별 집행기준",
             "원문": "기계장치(공구·기구, 비품, SW 등) — 창업기업이 직접 사용하는 것에 한한다.",
             "doc_id": "예비창업패키지 세부관리기준(2025년)"},
        ],
        "전제": [
            {"사실": "해당 인력이 협약상 참여인력이다", "근거조항": "S14",
             "매핑": ["F4.역할"], "미충족시": "불가"},
        ],
        "신뢰등급": "A",
        "버전스탬프": "제14차, 2025.12.23 기준",
        "참조사슬": [
            {"from": {"doc_id": "예비창업패키지 세부관리기준(2025년)", "조번호": "제22조"},
             "표기": "지침 제39조", "관계": "준용",
             "to": {"doc_id": "L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223",
                    "조번호": "제39조", "조제목": "기계장치"},
             "보정": None},
        ],
    },
    "조건부": {
        "판정": "조건부",
        "요약": "사전승인을 받으면 구매할 수 있습니다.",
        "해야할일": [
            # check_items 에 기계장치 사전승인 code 가 없어 code=None 이다.
            {"code": None,
             "항목": "사전승인 신청", "설명": "100만원 이상 기계장치는 주관기관 사전승인이 필요합니다."},
            {"code": "비교견적준비", "구분": "결제전", "유형": "기타",
             "항목": "비교견적 2곳 이상 확보", "설명": "50만원 이상 구매는 비교견적을 남깁니다."},
        ],
        "인용": [
            {"조번호": "제39조", "조제목": "기계장치",
             "원문": "기계장치는 사업계획서에 명시된 범위에서 취득할 수 있으며, 1인 1대를 원칙으로 한다.",
             "doc_id": "L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223"},
        ],
        "전제": [
            {"사실": "협약 총액이 확인되지 않았다", "근거조항": "S07",
             "매핑": ["F1.정부지원_현금"], "미충족시": "판단불가"},
        ],
        "신뢰등급": "B",
        "버전스탬프": "제14차, 2025.12.23 기준",
        "참조사슬": [],
    },
    "불가": {
        "판정": "불가",
        "요약": "개인 명의 구매는 사업비로 집행할 수 없습니다.",
        "해야할일": [
            # check_items 에 구매 명의 code 가 없다(출원인명의확인 은 무형자산 전용).
            {"code": None,
             "항목": "법인·사업자 명의로 재구매", "설명": "구매 명의가 사업자와 일치해야 합니다."},
        ],
        "인용": [
            {"조번호": "제36조", "조제목": "사업비의 집행",
             "원문": "사업비는 창업기업 명의의 사업비 관리계좌 및 카드로 집행하여야 한다.",
             "doc_id": "L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223"},
        ],
        "전제": [],
        "신뢰등급": "A",
        "버전스탬프": "제14차, 2025.12.23 기준",
        "참조사슬": [],
    },
    # 판단불가는 오류가 아니라 문의 초안으로 이어지는 정상 응답이다.
    "판단불가": {
        "판정": "판단불가",
        "요약": "이 지출을 판정할 근거 조항을 찾지 못했습니다. 주관기관에 문의가 필요합니다.",
        "해야할일": [],
        "인용": [],
        "전제": [
            {"사실": "협약 총액이 확인되지 않았다", "근거조항": None,
             "매핑": ["F1.정부지원_현금"], "미충족시": "판단불가"},
            {"사실": "해당 품목의 비목이 확정되지 않았다", "근거조항": None,
             "매핑": [], "미충족시": "판단불가"},
        ],
        "신뢰등급": "B",
        "버전스탬프": "제14차, 2025.12.23 기준",
        "참조사슬": [],
        "문의초안": ("안녕하세요. 예비창업패키지 참여기업입니다.\n\n"
                    "디자이너 업무용 노트북(250만원) 구매가 사업비로 집행 가능한지 문의드립니다.\n"
                    "통합관리지침 제39조(기계장치)의 '1인 1대' 기준에서 참여인력의 범위를 "
                    "어떻게 보아야 하는지 확인이 필요합니다.\n\n확인 부탁드립니다. 감사합니다."),
    },
}


def main() -> int:
    """계약 자가검사. 실패 건수가 있으면 1 을 돌려준다."""
    from fastapi.testclient import TestClient

    from server import inquiry, response_cache, routes_judge
    from server._common import MOCK, 판정_ENUM
    from server.main import app
    from server.models import 판정요청
    from server.rate_limit import 제한

    실패 = 0

    def 확인(설명, 조건, 부연=""):
        nonlocal 실패
        print(("PASS " if 조건 else "FAIL ") + 설명 + (f"  {부연}" if 부연 else ""))
        실패 += 0 if 조건 else 1

    if not MOCK:
        확인("SUDDOE_MOCK=1 로 실행해야 한다 (live 모드에서는 실제 판정 파이프라인을 부른다)", False)
        return 1

    c = TestClient(app)
    print("서버 계약 자가검사")

    print("\n[정적 엔드포인트]")
    h = c.get("/api/health").json()
    확인("GET /api/health", h.get("ok") is True, h.get("모드"))
    v = c.get("/api/vocab").json()
    확인("GET /api/vocab — 비목 10종", len(v["비목"]) == 10)
    v2 = c.get("/api/vocab?사업명=초기창업패키지").json()
    확인("창업활동비는 예비창업패키지에만", "창업활동비" not in v2["비목"] and len(v2["비목"]) == 9)
    p = c.get("/api/programs").json()
    확인("GET /api/programs — 8사업", len(p["사업"]) == 8, str(len(p["사업"])))

    print("\n[SSE]")

    def sse(경로, 본문=None):
        r = c.post(경로, json=본문) if 본문 is not None else c.post(경로)
        evts = {}
        이름 = None
        for 줄 in r.text.splitlines():
            if 줄.startswith("event: "):
                이름 = 줄[7:]
            elif 줄.startswith("data: ") and 이름:
                evts.setdefault(이름, []).append(json.loads(줄[6:]))
        return r, evts

    r, e = sse("/api/normalize", {"질문": "디자이너 쓸 맥북 250만원 사도 되나요?",
                                 "사업명": "예비창업패키지",
                                 "f5": {"친족거래": False, "전직임직원업체": False}})
    확인("POST /api/normalize 200 + text/event-stream",
         r.status_code == 200 and "event-stream" in r.headers.get("content-type", ""))
    n = (e.get("결과") or [{}])[0]
    확인("normalize 결과에 계약 필드 전부",
         all(k in n for k in ("품목", "금액", "금액_추정여부", "용도", "비목후보",
                              "결제수단", "구매명의", "신청일", "비교견적")))
    확인("normalize 완료 이벤트", bool(e.get("완료")))

    j: dict = {}
    for 원하는 in ("가능", "조건부", "불가", "판단불가"):
        r, e = sse(f"/api/judge?목={원하는}",
                   {"정규화": 목_정규화, "확정비목": "기계장치", "사업명": "예비창업패키지"})
        j = (e.get("결과") or [{}])[0]
        확인(f"judge ?목={원하는} → 판정 값", j.get("판정") == 원하는 and j.get("판정") in 판정_ENUM)
        if 원하는 == "가능":
            확인("judge 결과에 계약 필드 전부",
                 all(k in j for k in ("판정", "요약", "해야할일", "인용", "전제",
                                      "신뢰등급", "버전스탬프", "참조사슬")))
            확인("인용은 {조번호,조제목,원문,doc_id}",
                 all(set(x) >= {"조번호", "조제목", "원문", "doc_id"} for x in j["인용"]))
            확인("전제는 {사실,근거조항,매핑,미충족시}",
                 all(set(x) >= {"사실", "근거조항", "매핑", "미충족시"} for x in j["전제"]))
            확인("판정·해야할일·인용·전제·참조사슬이 각각 이벤트로 온다",
                 all(k in e for k in ("판정", "해야할일", "인용", "전제", "참조사슬")))
        if 원하는 == "판단불가":
            확인("판단불가 → 문의초안 이벤트", bool(e.get("문의초안")))
            확인("판단불가는 200 이다 (오류 아님)", r.status_code == 200)

    # 위 judge 검사는 전부 ?목= 고정 응답이다. 문의 초안은 live 경로 함수를 직접 호출해 확인한다.
    초안 = inquiry.문의초안(
        {"품목": "디자이너 업무용 노트북", "금액": 2500000}, "판단불가",
        [{"조번호": "제39조", "조제목": "기계장치", "doc_id": "L1_통합관리지침_20251223"}],
        [{"사실": "협약 총액이 확인되지 않았다"}], "예비창업패키지")
    확인("문의초안: live 경로 함수가 초안을 만든다", bool(초안))
    확인("문의초안: 판단불가가 아니면 만들지 않는다",
         all(inquiry.문의초안({"품목": "노트북", "금액": 2500000}, p, [], [], "예비창업패키지")
             is None for p in ("가능", "조건부", "불가")))
    확인("문의초안: 값이 없으면 지어내지 않는다 (빈 괄호·None·제0조 흔적 없음)",
         inquiry.문의초안({}, "판단불가", [], [], None) is None
         and not any(x in (inquiry.문의초안({"품목": "노트북"}, "판단불가", [],
                                          [{"사실": "비목이 확정되지 않았다"}], None) or "")
                     for x in ("()", "None", "미상", "제0조")))
    확인("확률·점수 필드가 없다",
         not any(k in j for k in ("확률", "점수", "confidence", "score")))

    # 목_판정 은 인용 키를 직접 갖고 있어 위 검사로는 live 경로가 판정 파이프라인의 키 이름
    # (인용목록·전제목록)을 읽는지 알 수 없다. 가짜 orchestrate 를 끼워 _실_판정 을 직접 부른다.
    print("\n[live 경로 판정 — 판정 파이프라인 결과 키를 읽는가]")
    import llm_schema

    def _가짜_판정(질문, **kw):
        인용 = llm_schema.인용(s번호="S14", doc_id="D1", 조번호="제39조",
                              조제목="기계장치", 원문="…", extraction="text")
        전제 = llm_schema.전제(사실="협약상 참여인력이다", 근거조항="S14",
                              매핑=["F4.역할"], 미충족시="불가")
        # 값은 전부 비지 않은 값이다. live 경로가 키 이름을 잘못 읽으면 기본값([]·None)이 나온다.
        r = llm_schema.최종응답(판정="조건부", 요약="자가검사용",
                              해야할일=[{"항목": "사전승인 신청", "설명": "…"}],
                              인용목록=[인용], 전제목록=[전제], 신뢰등급="B",
                              버전스탬프="자가검사",
                              참조사슬=[{"표기": "지침 제39조", "관계": "준용"}])
        return r.to_dict()

    가짜_orch = types.ModuleType("orchestrate")
    가짜_orch.판정 = _가짜_판정
    원래_orch = sys.modules.get("orchestrate")
    sys.modules["orchestrate"] = 가짜_orch
    try:
        live_out = routes_judge._실_판정(판정요청(
            정규화={"_원문": "디자이너가 쓸 맥북 프로 250만원", "비목후보": []},
            사업명="예비창업패키지"))
    finally:
        if 원래_orch is not None:
            sys.modules["orchestrate"] = 원래_orch
        else:
            sys.modules.pop("orchestrate", None)

    빈칸 = [k for k in 목_판정["가능"] if not live_out.get(k)]
    확인("live 경로(_실_판정)가 판정 파이프라인 결과 키를 전부 읽는다", not 빈칸,
         f"빈 키: {빈칸}" if 빈칸 else "")
    확인("live 경로 인용도 {조번호,조제목,원문,doc_id}",
         all(set(x) >= {"조번호", "조제목", "원문", "doc_id"} for x in live_out.get("인용", [])))

    print("\n[F 프로필 — f1 은 현금 2칸, 현물 없음]")
    pr = c.get("/api/profile").json()
    확인("f1 에 현물 칸이 없다",
         set(pr["f1"]) == {"정부지원_현금", "자기부담_현금", "협약시작일", "협약종료일"},
         str(sorted(pr["f1"])))
    좋음 = c.put("/api/profile", json={
        "f1": {"정부지원_현금": 40_000_000, "자기부담_현금": 10_000_000},
        "f3": [{"비목": "기계장치", "재원": "정부지원", "금액": 2_500_000}],
        "f4": [{"역할": "개발자", "타사업참여율": 30}]})
    확인("PUT /api/profile 200", 좋음.status_code == 200)
    확인("f3 에 형태(현금/현물) 칸이 없다", "형태" not in 좋음.json()["받은값"]["f3"][0])
    확인("f4 에 이름 칸이 없다", "이름" not in 좋음.json()["받은값"]["f4"][0])
    나쁨 = c.put("/api/profile", json={"f3": [{"비목": "기계장치", "재원": "정부지원",
                                              "형태": "현물", "금액": 1}]})
    확인("f3.형태 를 보내도 무시된다 (스키마에 없다)",
         나쁨.status_code == 200 and "형태" not in 나쁨.json()["받은값"]["f3"][0])

    print("\n[입력 검증]")
    확인("비목 enum 밖은 422",
         c.post("/api/judge", json={"정규화": {}, "확정비목": "회식비"}).status_code == 422)
    확인("빈 질문은 422",
         c.post("/api/normalize", json={"질문": ""}).status_code == 422)

    print("\n[요청 제한·캐시·관리 엔드포인트]")
    확인("관리 엔드포인트는 토큰 없이 403", c.get("/admin/cost").status_code == 403)
    # HTTP 헤더는 ASCII 만 보낼 수 있어 관리 토큰도 ASCII 로 둔다.
    os.environ["SUDDOE_ADMIN_TOKEN"] = "selftest-token"
    관리 = {"X-Admin-Token": "selftest-token"}
    확인("토큰이 맞으면 200", c.get("/admin/cost", headers=관리).status_code == 200)

    # 여러 사용자가 같은 예시 질문을 누르는 경우.
    본 = {"질문": "같은 질문 캐시 확인", "사업명": "예비창업패키지"}
    전 = response_cache.상태()["적중"]
    _, e1 = sse("/api/normalize", 본)
    _, e2 = sse("/api/normalize", 본)
    확인("같은 질문은 캐시로 답한다",
         response_cache.상태()["적중"] == 전 + 1 and e2["완료"][0]["캐시"] is True)
    확인("캐시 응답이 첫 응답과 같다", e1["결과"][0] == e2["결과"][0])
    확인("캐시 적중 수가 /admin/cost 에 나온다",
         c.get("/admin/cost", headers=관리).json()["캐시"]["적중"] > 0)

    q = c.get("/admin/queue", headers=관리).json()
    확인("/admin/queue 가 재검수 큐를 준다",
         "요약" in q and "항목" in q and "상태별" in q, f"대기 {q.get('전체')}건")
    확인("/admin/queue 는 토큰 없이 403", c.get("/admin/queue").status_code == 403)
    q2 = c.get("/admin/queue?종류=A4개정&limit=5", headers=관리).json()
    확인("/admin/queue 필터가 적용된다",
         all(x["종류"] == "A4개정" for x in q2["항목"]) and len(q2["항목"]) <= 5)

    한도전 = 제한.IP시간당
    제한.IP시간당 = 1
    제한._ip.clear()
    c.post("/api/normalize", json={"질문": "첫 번째"})
    막힘 = c.post("/api/normalize", json={"질문": "두 번째"})
    확인("IP 시간당 한도를 넘으면 429", 막힘.status_code == 429, str(막힘.json().get("오류"))[:40])
    제한.IP시간당 = 한도전
    제한._ip.clear()

    캡전 = 제한.일일캡
    제한.일일캡 = 0
    확인("일일 한도를 넘으면 429",
         c.post("/api/normalize", json={"질문": "한도 초과"}).status_code == 429)
    제한.일일캡 = 캡전

    제한.운영시작 = "2099-01-01T00:00:00+00:00"
    기간밖 = c.post("/api/normalize", json={"질문": "운영 기간 밖"})
    확인("운영 기간 밖이면 429", 기간밖.status_code == 429, str(기간밖.json().get("오류"))[:40])
    제한.운영시작 = ""

    print(f"\n{'PASS 전부 통과' if not 실패 else f'FAIL {실패}건'}")
    return 1 if 실패 else 0


if __name__ == "__main__":
    sys.exit(main())
