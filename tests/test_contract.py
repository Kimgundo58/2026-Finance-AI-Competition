# -*- coding: utf-8 -*-
"""레인 D — `프론트_API_계약_v1.0.md` 를 **집행 가능한 테스트로** 바꾼 것.

    PYTHONIOENCODING=utf-8 python -m pytest tests/test_contract.py -q

🔴 **이 파일은 계약을 «검증» 만 한다. 결함을 고치지 않는다.**
   `server/` 는 레인 A·B·C 가 동시에 쓰고 있다. 깨지면 조율 세션에 보고한다.

■ 왜 필요한가
  계약 문서는 사람만 읽을 수 있어서, 레인이 필드를 하나 빼먹어도 아무도 모른다.
  여기서 **키 집합을 양방향으로** 대조한다 — 빠진 키뿐 아니라 **계약에 없는 키가
  늘어난 것도** 잡는다. 프론트가 계약 밖 필드를 쓰기 시작하는 게 다음 사고다.

■ 대조 대상이 둘이다. 둘 다 봐야 한다
  ① **모델 필드 집합** (`server/models.py`) ↔ 계약 문서 표
     라우터 대부분이 `response_model` 을 걸어 두어 **응답에는 계약 밖 키가 애초에
     안 실린다.** 즉 응답만 보면 «필드가 늘어난 것» 을 영원히 못 잡는다.
     늘어남은 모델에서 잡아야 한다.
  ② **실제 응답 키** ↔ 계약 문서 표
     `response_model` 이 없는 경로(`/api/health`·`/api/vocab`·`/api/programs`·
     `/api/profile`)와 **SSE 이벤트**가 여기 걸린다. SSE 는 생 dict 라 계약을
     지켜 줄 장치가 하나도 없다 — 이 파일에서 제일 값어치가 큰 자리다.

■ 목/실 두 모드
  같은 파일에서 둘 다 본다. 파일 상단 `실DB = False` 라 `conftest` 는 매 테스트를
  목으로 세우고, 실 모드가 필요한 테스트만 `모드(목=False)` 로 뒤집는다.
  §9 「실서버로 갈아끼울 때 버그로 오해할 것」의 실질 보증이 이것이다.
"""
from __future__ import annotations

import contextlib
import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient          # noqa: E402

from server import models, routes_l3, routes_plans, routes_tasks   # noqa: E402
from server._common import (계획상태_ENUM, 비목_ENUM, 판정_ENUM,   # noqa: E402
                            할일구분_ENUM, 할일상태_ENUM, 할일유형_ENUM, _질의)
from server.main import app, 가드                                   # noqa: E402

# conftest 에 «이 파일은 목이다» 라고 알린다. 실 모드는 아래 `모드()` 로 직접 뒤집는다.
실DB = False

client = TestClient(app)

_모듈 = ("server._common", "server.routes_plans", "server.routes_tasks",
        "server.routes_l3", "server.main")


@contextlib.contextmanager
def 모드(목: bool):
    """`MOCK` 은 import 시점 상수라 환경변수로는 못 바꾼다 — 속성을 직접 뒤집는다."""
    이전 = {}
    for 이름 in _모듈:
        m = sys.modules.get(이름) or importlib.import_module(이름)
        if hasattr(m, "MOCK"):
            이전[이름] = m.MOCK
            m.MOCK = 목
    try:
        yield
    finally:
        for 이름, v in 이전.items():
            sys.modules[이름].MOCK = v


@pytest.fixture(autouse=True)
def _가드_초기화():
    """IP 시간당 60건 캡과 캐시가 테스트 순서에 결과를 물들이지 않게 한다."""
    가드._ip.clear()
    가드._캐시.clear()
    가드.오늘_호출 = 0
    yield


# ════════════════════════════════════════════════════════════════════
# 계약 문서 표 — 여기가 이 파일의 «정답». 문서를 고치면 여기도 고친다.
# ════════════════════════════════════════════════════════════════════

# (필수 키, 있어도 되는 키)
계약 = {
    "health":      ({"ok", "모드", "판정_enum", "시각"}, set()),
    "vocab":       ({"비목", "별칭", "사업명", "비고"}, set()),
    # 🔴 `비고` 는 corpus.programs 를 못 읽었을 때만 붙는다 (코드 상수 폴백)
    "programs":    ({"사업"}, {"비고"}),
    "programs_항": ({"사업명", "별칭", "비목계통", "트랙범위"}, set()),
    "계획목록":     ({"통계", "건수", "페이지", "크기", "항목"}, set()),
    "계획통계":     ({"전체", "확인필요", "위험", "특이사항없음", "점검전", "금액합계"}, set()),
    "계획요약":     ({"plan_id", "제목", "확정비목", "금액", "판정",
                    "집행예정일", "updated_at", "사업명", "상태"}, set()),
    "계획상세추가":  ({"질문원문", "용도", "거래처", "추가설명", "정규화",
                    "latest_decision_id", "판정상세", "할일", "created_at"}, set()),
    "할일":        ({"task_id", "plan_id", "출처", "코드", "구분", "항목", "설명",
                    "due_date", "유형", "날짜_사용자수정", "상태", "계획제목"}, set()),
    "할일목록":     ({"건수", "항목"}, set()),
    "동기화":       ({"생성", "갱신", "보존_user", "보존_날짜수정", "코드매칭", "코드미상"}, set()),
    "L3":          ({"doc_id", "파일명", "확장자", "상태", "조_건수", "dangling", "메시지"}, set()),
    "프로필":       ({"f1", "f3", "f4"}, set()),
    # SSE 생 dict — response_model 이 안 지켜 준다
    # 🔴 `하위항목` 은 2026-09-01 까지 목·폼 경로에만 없어서 «모드마다 다른 키» 였다.
    #    레인 C 가 `_실_정규화` 의 **공통** setdefault 로 메워 세 경로가 같아졌다 →
    #    선택에서 **필수로 올린다.** 다시 빠지면 여기서 걸린다.
    "정규화결과":    ({"품목", "금액", "금액_추정여부", "용도", "비목후보", "하위항목",
                    "결제수단", "구매명의", "신청일", "비교견적", "질문원문"}, set()),
    # 🔴 `받은값` 은 계약이 아니다 (조율 세션 2026-09-01 판정 — 디버그 잔재)
    "프로필저장":    ({"저장"}, {"profile_id", "이유"}),
    "판정이벤트":    ({"판정", "요약", "신뢰등급", "버전스탬프"}, set()),
    "판정결과":     ({"판정", "요약", "해야할일", "인용", "전제",
                    "신뢰등급", "버전스탬프", "참조사슬"}, {"문의초안"}),
}


def _키대조(이름: str, 실제: dict, 맥락: str = ""):
    """빠진 키와 «늘어난» 키를 한 번에 잡는다."""
    필수, 선택 = 계약[이름]
    있음 = set(실제)
    빠짐 = 필수 - 있음
    늘어남 = 있음 - 필수 - 선택
    assert not 빠짐, f"[{이름}{맥락}] 계약 키가 빠졌다: {sorted(빠짐)}"
    assert not 늘어남, (f"[{이름}{맥락}] 계약에 없는 키가 늘었다: {sorted(늘어남)} "
                      f"— models.py 를 고쳤으면 계약 문서와 이 파일도 같이 고쳐야 한다")


# ════════════════════════════════════════════════════════════════════
# ① 모델 필드 ↔ 계약   — «늘어남» 은 여기서만 잡힌다
# ════════════════════════════════════════════════════════════════════

def _필드(모델) -> set[str]:
    return set(모델.model_fields)


def test_모델_필드가_계약과_같다():
    _키대조("계획요약", dict.fromkeys(_필드(models.계획요약)))
    _키대조("계획통계", dict.fromkeys(_필드(models.계획통계)))
    _키대조("계획목록", dict.fromkeys(_필드(models.계획목록응답)))
    _키대조("할일", dict.fromkeys(_필드(models.할일)))
    _키대조("할일목록", dict.fromkeys(_필드(models.할일목록응답)))
    _키대조("동기화", dict.fromkeys(_필드(models.할일동기화응답)))
    _키대조("L3", dict.fromkeys(_필드(models.L3업로드응답)))
    _키대조("프로필", dict.fromkeys(_필드(models.프로필)))

    # 상세 = 요약 + 추가 9개. 상속이라 따로 센다
    추가 = _필드(models.계획상세) - _필드(models.계획요약)
    _키대조("계획상세추가", dict.fromkeys(추가))


def test_정규화응답_모델이_계약과_같다():
    """🔴 `하위항목` 은 모델에 있는데 SSE 결과에는 안 실린다 — 아래 SSE 테스트 참조."""
    _키대조("정규화결과", dict.fromkeys(_필드(models.정규화응답)))


def test_오류응답_모델은_두칸뿐이다():
    assert _필드(models.오류응답) == {"오류", "상태"}


# ════════════════════════════════════════════════════════════════════
# ② 실제 응답 키 ↔ 계약   (response_model 없는 경로 · 목 모드)
# ════════════════════════════════════════════════════════════════════

def test_health_vocab_programs_profile_키():
    _키대조("health", client.get("/api/health").json())
    _키대조("vocab", client.get("/api/vocab").json())

    j = client.get("/api/programs").json()
    _키대조("programs", j)
    for 항 in j["사업"]:
        _키대조("programs_항", 항)

    _키대조("프로필", client.get("/api/profile").json())


@pytest.mark.xfail(reason="`받은값` 은 디버그 잔재 — 계약 아님(조율 세션 2026-09-01 동결). "
                          "응답은 {저장, profile_id} / {저장, 이유} 다. 걷히면 초록",
                   strict=False)
def test_프로필_저장_응답이_동결_모양이다():
    """🔴 `받은값` 은 보낸 프로필 전문을 되돌려준다. 프론트가 그걸 읽기 시작하면
    실 경로로 갈아끼울 때 사라져서 깨진다 — 계약이 아닌 것에 코드를 얹는 사고다."""
    j = client.put("/api/profile", json={}).json()
    _키대조("프로필저장", j)
    assert isinstance(j["저장"], bool)
    if j["저장"] is False:
        assert j.get("이유"), "저장 실패면 이유가 있어야 한다"


def test_계획_목록과_상세_키():
    j = client.get("/api/plans").json()
    _키대조("계획목록", j)
    _키대조("계획통계", j["통계"])
    for r in j["항목"]:
        _키대조("계획요약", r, f" plan_id={r.get('plan_id')}")

    d = client.get("/api/plans/1").json()
    필수, _ = 계약["계획요약"]
    추가, _ = 계약["계획상세추가"]
    assert set(d) == 필수 | 추가, f"상세 키 불일치: {sorted(set(d) ^ (필수 | 추가))}"


def test_할일_목록과_추가와_수정_키():
    j = client.get("/api/tasks").json()
    _키대조("할일목록", j)
    for t in j["항목"]:
        _키대조("할일", t, f" task_id={t.get('task_id')}")

    r = client.post("/api/plans/1/tasks",
                    json={"항목": "견적서 3부", "구분": "결제전", "유형": "기타"})
    assert r.status_code == 201, r.text
    _키대조("할일", r.json(), " POST")

    r = client.patch("/api/plans/1/tasks/11", json={"상태": "완료"})
    assert r.status_code == 200, r.text
    _키대조("할일", r.json(), " PATCH")

    r = client.post("/api/plans/1/tasks:sync", json={"해야할일": []})
    assert r.status_code == 200, r.text
    _키대조("동기화", r.json())


def test_L3_업로드와_상태_키():
    r = client.post("/api/l3/upload",
                    files={"파일": ("규정.hwpx", b"x" * 32, "application/octet-stream")},
                    data={"org_id": "org-계약테스트"})
    assert r.status_code == 202, r.text
    _키대조("L3", r.json(), " upload")
    _키대조("L3", client.get("/api/l3/l3-mock-0002").json(), " 상태")


# ════════════════════════════════════════════════════════════════════
# ③ 목 ↔ 실 — 같은 키가 나오는가 (§9 의 실질 보증)
# ════════════════════════════════════════════════════════════════════

_모드무관 = ["/api/health", "/api/vocab", "/api/programs", "/api/profile",
           "/api/plans", "/api/tasks"]


def test_목과_실이_같은_키를_준다():
    """🔴 값이 아니라 **키**를 본다. 실 모드에서 0건이어도 봉투 모양은 같아야 한다.

    `_common._질의` 는 DB 접속 실패 시 조용히 빈 리스트를 준다 — 그래서 이 테스트는
    DB 없이도 «봉투가 같은가» 를 검증한다. 항목 «내용» 비교는 DB 가 있을 때만 한다.
    """
    with 모드(목=True):
        목결과 = {p: client.get(p).json() for p in _모드무관}
    with 모드(목=False):
        실결과 = {p: client.get(p).json() for p in _모드무관}

    for p in _모드무관:
        ㅁ, ㅅ = 목결과[p], 실결과[p]
        assert set(ㅁ) == set(ㅅ), (f"{p} 봉투 키가 모드에 따라 다르다 — "
                                   f"프론트가 실서버로 갈아끼울 때 깨진다: "
                                   f"목만 {sorted(set(ㅁ) - set(ㅅ))} · "
                                   f"실만 {sorted(set(ㅅ) - set(ㅁ))}")
        for 키 in ("항목", "통계"):
            if isinstance(ㅁ.get(키), list) and ㅁ[키] and 실결과[p].get(키):
                assert set(ㅁ[키][0]) == set(ㅅ[키][0]), f"{p}.{키}[0] 키가 모드마다 다르다"
            if isinstance(ㅁ.get(키), dict) and isinstance(ㅅ.get(키), dict):
                assert set(ㅁ[키]) == set(ㅅ[키]), f"{p}.{키} 키가 모드마다 다르다"


def test_정규화_SSE_가_목과_실에서_같은_키를_준다():
    """폼 경로는 실 모드에서도 LLM 을 안 탄다 (`_실_정규화` 의 else 가지) — GPU 없이 돈다."""
    본문 = {"품목": "맥북", "금액": 2500000, "용도": "디자이너 작업용",
           "사업명": "초기창업패키지"}
    with 모드(목=True):
        목 = dict(_sse수집("/api/normalize", 본문))["결과"]
    with 모드(목=False):
        실 = dict(_sse수집("/api/normalize", 본문))["결과"]
    assert set(목) == set(실), (f"정규화 결과 키가 모드마다 다르다: "
                               f"목만 {sorted(set(목) - set(실))} · 실만 {sorted(set(실) - set(목))}")


# ════════════════════════════════════════════════════════════════════
# ④ 폐쇄 enum 이 새는가
# ════════════════════════════════════════════════════════════════════

def test_판정은_4way_와_null_뿐이다():
    허용 = set(판정_ENUM) | {None}
    for r in client.get("/api/plans").json()["항목"]:
        assert r["판정"] in 허용, f"판정 어휘가 샜다: {r['판정']}"
        assert r["상태"] in 계획상태_ENUM, f"계획 상태 어휘가 샜다: {r['상태']}"
    assert set(client.get("/api/health").json()["판정_enum"]) == set(판정_ENUM)


@pytest.mark.parametrize("목판정", 판정_ENUM)
def test_judge_4way_전부_그려진다(목판정):
    """`?목=` 로 네 화면을 다 만들 수 있어야 한다 (§6). 판단불가는 에러가 아니다."""
    이벤트 = _sse수집("/api/judge", {}, f"?목={목판정}")
    결과 = dict(이벤트)["결과"]
    assert 결과["판정"] == 목판정
    assert 결과["판정"] in 판정_ENUM


def test_할일_세_어휘축이_안_섞인다():
    for t in client.get("/api/tasks").json()["항목"]:
        assert t["상태"] in 할일상태_ENUM, f"할일.상태 어휘가 샜다: {t['상태']}"
        assert t["구분"] in 할일구분_ENUM, f"할일.구분 어휘가 샜다: {t['구분']}"
        assert t["유형"] in 할일유형_ENUM, f"할일.유형 어휘가 샜다: {t['유형']}"
        assert t["출처"] in ("ai", "user")


def test_비목은_10종_폐쇄다():
    전체 = client.get("/api/vocab").json()["비목"]
    assert set(전체) == set(비목_ENUM), f"비목 enum 이 바뀌었다: {set(전체) ^ set(비목_ENUM)}"

    # 창업활동비는 예비창업패키지에만 있다
    예비 = client.get("/api/vocab?사업명=예비창업패키지").json()["비목"]
    초기 = client.get("/api/vocab?사업명=초기창업패키지").json()["비목"]
    assert "창업활동비" in 예비
    assert "창업활동비" not in 초기

    for r in client.get("/api/plans").json()["항목"]:
        if r["확정비목"] is not None:
            assert r["확정비목"] in 비목_ENUM, f"목 데이터의 비목이 enum 밖이다: {r['확정비목']}"


def test_비목_enum_밖은_422():
    r = client.post("/api/judge", json={"확정비목": "회의비"})
    assert r.status_code == 422, r.text


# ════════════════════════════════════════════════════════════════════
# ⑤ 오류는 전부 한 모양인가
# ════════════════════════════════════════════════════════════════════

def _오류모양(r, 상태: int, 필드있음: bool = False):
    assert r.status_code == 상태, f"{상태} 를 기대했는데 {r.status_code}: {r.text[:200]}"
    j = r.json()
    기대 = {"오류", "상태", "필드"} if 필드있음 else {"오류", "상태"}
    assert set(j) == 기대, f"오류 봉투가 계약과 다르다: {sorted(set(j) ^ 기대)} — {j}"
    assert j["상태"] == 상태
    assert isinstance(j["오류"], str) and j["오류"]
    assert "detail" not in j, "pydantic 기본 형식이 샜다 — 프론트가 렌더러를 두 벌 만들게 된다"


def test_404_오류봉투():
    _오류모양(client.get("/api/plans/999999"), 404)


def test_415_413_400_오류봉투():
    def 업로드(이름, 본문=b"x" * 32):
        return client.post("/api/l3/upload",
                           files={"파일": (이름, 본문, "application/octet-stream")},
                           data={"org_id": "org-계약테스트"})
    _오류모양(업로드("규정.docx"), 415)
    _오류모양(업로드("규정.doc"), 415)
    _오류모양(업로드("규정.zip"), 415)
    _오류모양(업로드("규정.pdf", b""), 400)
    _오류모양(업로드("규정.pdf", b"x" * (30 * 1024 * 1024 + 1)), 413)


def test_403_오류봉투():
    _오류모양(client.get("/admin/cost"), 403)


def test_422는_필드가_한칸_더_붙는다():
    # 폼도 자연어도 없다
    _오류모양(client.post("/api/normalize", json={}), 422, 필드있음=True)
    # 🔴 질문(자연어) 과 폼을 동시에 보내면 422
    _오류모양(client.post("/api/normalize", json={
        "질문": "노트북 사도 되나요", "품목": "맥북", "금액": 2500000, "용도": "개발"}),
        422, 필드있음=True)
    # 폼이 반만 왔다
    _오류모양(client.post("/api/normalize", json={"품목": "맥북"}), 422, 필드있음=True)


def test_422_필드는_body_를_안_흘린다():
    j = client.post("/api/normalize", json={}).json()
    assert j["필드"] is None or not str(j["필드"]).startswith("body")


# ════════════════════════════════════════════════════════════════════
# ⑥ SSE 이벤트 «순서»
# ════════════════════════════════════════════════════════════════════

def _sse수집(경로: str, 본문: dict, 쿼리: str = "") -> list[tuple[str, object]]:
    """(이벤트명, 값) 을 «온 순서대로» 돌려준다. 순서가 계약이다."""
    r = client.post(경로 + 쿼리, json=본문)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    out, 이름 = [], None
    for 줄 in r.text.splitlines():
        if 줄.startswith("event: "):
            이름 = 줄[7:].strip()
        elif 줄.startswith("data: ") and 이름 is not None:
            out.append((이름, json.loads(줄[6:])))
            이름 = None
    return out


def test_normalize_SSE_순서():
    이벤트 = _sse수집("/api/normalize", {"품목": "맥북", "금액": 2500000,
                                     "용도": "디자이너 작업용", "사업명": "초기창업패키지"})
    이름 = [n for n, _ in 이벤트]
    assert 이름[0] == "진행", 이름
    assert 이름[-2:] == ["결과", "완료"], f"결과 → 완료 로 끝나야 한다: {이름}"
    assert set(이름[1:-2]) <= {"필드"}, f"진행과 결과 사이엔 필드만 온다: {이름}"
    _키대조("정규화결과", dict(이벤트)["결과"])


def test_normalize_는_결과_하나만_들어도_화면이_그려진다():
    """§5 «결과 하나만 들어도 된다» — 필드 이벤트를 다 버려도 정보가 안 준다."""
    이벤트 = _sse수집("/api/normalize", {"품목": "맥북", "금액": 2500000,
                                     "용도": "디자이너 작업용", "사업명": "초기창업패키지"})
    d = dict(이벤트)
    for 이름, 값 in 이벤트:
        if 이름 == "필드":
            for k, v in 값.items():
                assert k in d["결과"] and d["결과"][k] == v, f"필드 {k} 가 결과에 없다"


# 계약 §6. 🔴 `저장` 은 2026-09-01 조율 세션이 계약에 넣은 판정 영속화 배선이다.
_판정순서 = ["판정", "해야할일", "인용", "전제", "참조사슬", "결과", "저장", "완료"]


@pytest.mark.parametrize("목판정", 판정_ENUM)
def test_judge_SSE_순서(목판정):
    이벤트 = _sse수집("/api/judge", {}, f"?목={목판정}")
    이름 = [n for n, _ in 이벤트]

    앞 = [n for n in 이름 if n == "진행"]
    assert 앞, "진행 이벤트가 하나도 없다"
    assert 이름[:len(앞)] == 앞, f"진행은 앞에 몰려 와야 한다: {이름}"

    뒤 = [n for n in 이름[len(앞):] if n != "문의초안"]
    assert 뒤 == _판정순서, f"판정 SSE 순서가 계약과 다르다: {뒤}"

    _키대조("판정이벤트", dict(이벤트)["판정"])
    _키대조("판정결과", dict(이벤트)["결과"])


def test_판단불가면_문의초안이_참조사슬과_결과_사이에_온다():
    이름 = [n for n, _ in _sse수집("/api/judge", {}, "?목=판단불가")]
    assert "문의초안" in 이름, "판단불가인데 문의초안이 없다 — 화면 9 로 이을 재료가 없다"
    assert 이름.index("참조사슬") < 이름.index("문의초안") < 이름.index("결과")


@pytest.mark.parametrize("목판정", ("가능", "조건부", "불가"))
def test_판단불가가_아니면_문의초안이_없다(목판정):
    이름 = [n for n, _ in _sse수집("/api/judge", {}, f"?목={목판정}")]
    assert "문의초안" not in 이름


def test_judge_SSE_에_저장_이벤트가_있다():
    """계약 §6 순서는 … 참조사슬 → 결과 → **저장** → 완료 다."""
    이름 = [n for n, _ in _sse수집("/api/judge", {}, "?목=가능")]
    assert "저장" in 이름
    assert 이름.index("결과") < 이름.index("저장") < 이름.index("완료")


# 🔴 `저장: false` 는 실패가 아니다. 아래 셋은 전부 **정상 경로**다 —
#    프론트가 이걸 빨간 배너로 그리면 안 된다 (조율 세션 확인, 2026-09-01).
_저장_정상사유 = {"plan_id 없음", "캐시 적중 — 새 판정 기록 없음",
               "decision_id 없음 — 판정이 기록되지 않았다"}


def test_저장_이벤트는_plan_id_없으면_정상적으로_거른다():
    저장 = dict(_sse수집("/api/judge", {}, "?목=가능"))["저장"]
    assert 저장["저장"] is False
    assert 저장.get("사유") in _저장_정상사유, f"모르는 저장 실패 사유: {저장}"


def test_캐시_적중은_저장을_건너뛴다_정상경로다():
    """같은 요청을 두 번 — 두 번째는 캐시다. 캐시 적중이면 새 판정 기록이 없으므로
    저장하지 않는다. 🔴 저장하면 다른 계획에 남의 판정이 붙는다."""
    본문 = {"plan_id": 1, "사업명": "초기창업패키지"}
    첫 = dict(_sse수집("/api/judge", 본문, "?목=가능"))
    둘 = dict(_sse수집("/api/judge", 본문, "?목=가능"))
    assert 둘["완료"]["캐시"] is True, f"두 번째 호출이 캐시를 안 탔다: {둘['완료']}"
    assert 둘["저장"]["저장"] is False
    assert 둘["저장"].get("사유") in _저장_정상사유
    assert 첫["결과"] == 둘["결과"], "캐시 응답이 원본과 다르다"


@pytest.mark.parametrize("목판정", 판정_ENUM)
def test_결과_이벤트에_decision_id_가_실리지_않는다(목판정):
    """🔴 TENANT_LEAK 류 방어. `decision_id` 가 결과·캐시에 박히면 다른 요청이
    남의 판정 기록을 자기 계획에 가리키게 된다. 내부 키는 결과 전에 벗겨야 한다."""
    이벤트 = dict(_sse수집("/api/judge", {"plan_id": 1}, f"?목={목판정}"))
    결과 = 이벤트["결과"]
    샌키 = [k for k in 결과 if "decision" in k.lower()]
    assert not 샌키, f"결과 이벤트에 내부 키가 샜다: {샌키}"
    assert "decision_id" not in 이벤트["판정"]


def test_SSE_는_실패해도_500_을_안_던진다(monkeypatch):
    """🔴 모든 실패의 기본값은 판단불가다. 빨간 화면이 아니라 안내 화면이다."""
    import server.main as m

    def 터짐(body):
        raise RuntimeError("일부러")

    monkeypatch.setattr(m, "_실_정규화", 터짐)
    with 모드(목=False):
        이름 = [n for n, _ in _sse수집("/api/normalize", {"질문": "노트북 사도 되나요"})]
    assert 이름[-2:] == ["오류", "완료"], 이름


# ════════════════════════════════════════════════════════════════════
# ⑦ 🔴 vlm 출처는 A등급으로 인용될 수 없다
#     (CLAUDE.md — 스캔 판독본은 `extraction='vlm'` 태깅, **A등급 인용 금지**)
#     계약 문서 §8 「만들지 않는 것」에는 아직 안 적혀 있다. 여기서 먼저 박는다.
# ════════════════════════════════════════════════════════════════════

def _vlm_문서() -> set[str]:
    """코퍼스에서 실제 vlm 로 태깅된 doc_id. DB 가 없으면 빈 집합."""
    return {r[0] for r in _질의("SELECT doc_id FROM corpus.documents WHERE extraction = 'vlm'")}


def _A등급_vlm위반(결과: dict, vlm: set[str]) -> list[str]:
    """A등급 판정이 vlm 출처를 인용하면 그 doc_id 를 돌려준다."""
    if 결과.get("신뢰등급") != "A":
        return []
    return [c.get("doc_id") for c in 결과.get("인용", []) if c.get("doc_id") in vlm]


def test_검사기가_실제로_잡는다():
    """🔴 이 테스트가 없으면 위 검사는 «vlm 이 안 섞였다» 가 아니라 «아무것도 안 본다»
       와 구별이 안 된다. 알려진 위반을 넣어 검사기에 이빨이 있는지부터 본다."""
    가짜 = {"신뢰등급": "A", "인용": [{"doc_id": "스캔본_X"}]}
    assert _A등급_vlm위반(가짜, {"스캔본_X"}) == ["스캔본_X"]
    assert _A등급_vlm위반({"신뢰등급": "B", "인용": [{"doc_id": "스캔본_X"}]}, {"스캔본_X"}) == []


@pytest.mark.parametrize("목판정", 판정_ENUM)
def test_A등급_판정은_vlm_을_인용하지_않는다(목판정):
    vlm = _vlm_문서()
    if not vlm:
        pytest.skip("코퍼스 DB 미기동 — vlm 문서 목록을 못 읽는다")
    결과 = dict(_sse수집("/api/judge", {}, f"?목={목판정}"))["결과"]
    위반 = _A등급_vlm위반(결과, vlm)
    assert not 위반, (f"A등급 판정이 스캔 판독본을 인용했다: {위반} — "
                     f"vlm 은 A등급 인용 금지 (CLAUDE.md 파싱 절)")


def test_인용은_원문_문자열_필드를_따로_안_만든다():
    """인용은 S번호 추출이다 — LLM 이 원문을 쓰지 않는다. 인용 항목의 `원문` 은
    **코드가 코퍼스에서 떠 온 것**이라야 하고, 그 자리에 doc_id 가 반드시 따라야 한다."""
    결과 = dict(_sse수집("/api/judge", {}, "?목=가능"))["결과"]
    for c in 결과["인용"]:
        assert c.get("doc_id"), f"인용에 출처 doc_id 가 없다: {c}"
        assert c.get("조번호"), f"인용에 조번호가 없다: {c}"


# ════════════════════════════════════════════════════════════════════
# ⑧ 실 경로 실패가 계약 밖 키를 흘리는가
# ════════════════════════════════════════════════════════════════════

def test_실판정_실패시에도_계약_키만_나온다(monkeypatch):
    """🔴 2026-09-01 까지는 `_오류`(예외 클래스명)가 결과에 실려 xfail 이었다.
    레인 C 가 걷고 대신 `logging` 으로 옮겼다 — **지운 게 아니라 자리를 옮긴 것**이라
    진단은 서버 로그에 남는다. 다시 응답으로 새면 여기서 걸린다."""
    import server.main as m

    def 터짐(body):
        raise RuntimeError("일부러")

    monkeypatch.setattr(m, "_실_판정", 터짐)
    with 모드(목=False):
        결과 = dict(_sse수집("/api/judge", {}))["결과"]
    assert 결과["판정"] == "판단불가"          # 여기까지는 지켜진다
    _키대조("판정결과", 결과)                  # 여기서 `_오류` 가 걸린다


def test_실판정이_터져도_4way_밖으로_안_나간다(monkeypatch):
    """계약 밖 키와 별개로, **판정 어휘는 절대 안 샌다.** 이건 지켜져야 한다."""
    import server.main as m

    def 터짐(body):
        raise RuntimeError("일부러")

    monkeypatch.setattr(m, "_실_판정", 터짐)
    with 모드(목=False):
        결과 = dict(_sse수집("/api/judge", {}))["결과"]
    assert 결과["판정"] == "판단불가"
    assert 결과["판정"] in 판정_ENUM


# ════════════════════════════════════════════════════════════════════
# ⑨ 엔드포인트가 계약 §1 표대로 다 있는가
# ════════════════════════════════════════════════════════════════════

계약_엔드포인트 = {
    ("GET", "/api/health"), ("GET", "/api/vocab"), ("GET", "/api/programs"),
    ("POST", "/api/normalize"), ("POST", "/api/judge"),
    ("GET", "/api/plans"), ("POST", "/api/plans"), ("GET", "/api/plans/{plan_id}"),
    ("POST", "/api/plans/{plan_id}/tasks:sync"),
    ("POST", "/api/plans/{plan_id}/tasks"),
    ("PATCH", "/api/plans/{plan_id}/tasks/{task_id}"),
    ("GET", "/api/tasks"),
    ("POST", "/api/l3/upload"), ("GET", "/api/l3/{doc_id}"),
    ("GET", "/api/profile"), ("PUT", "/api/profile"),
}


def _실제_엔드포인트() -> set[tuple[str, str]]:
    """🔴 `app.routes` 로 세면 안 된다.

    이 FastAPI 버전의 `include_router` 는 라우트를 평탄화하지 않고 포함 라우터 객체
    하나만 `app.routes` 에 넣는다(지연 결합) — 하위 라우트가 `path=None` 으로 보여
    **레인 A·B·C 의 엔드포인트 9개가 통째로 «없음» 으로 잡힌다.**
    OpenAPI 스펙이 «프론트가 실제로 보는 것» 이기도 하니 그쪽을 정본으로 쓴다.
    """
    스펙 = app.openapi()["paths"]
    return {(m.upper(), p) for p, 메서드 in 스펙.items() for m in 메서드
            if m.upper() not in ("HEAD", "OPTIONS")}


def test_계약_엔드포인트가_전부_살아있다():
    빠짐 = 계약_엔드포인트 - _실제_엔드포인트()
    assert not 빠짐, f"계약 §1 표에 있는데 서버에 없다: {sorted(빠짐)}"


def test_계약에_없는_api_가_늘지_않았다():
    늘어남 = {(m, p) for m, p in _실제_엔드포인트()
            if p.startswith("/api")} - 계약_엔드포인트
    assert not 늘어남, (f"계약 문서에 없는 /api 경로가 생겼다: {sorted(늘어남)} — "
                      f"프론트가 계약 밖 경로를 쓰기 시작하면 그게 다음 사고다")
