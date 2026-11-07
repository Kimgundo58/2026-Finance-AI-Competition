# -*- coding: utf-8 -*-
"""판정 효과 비교(P5-2, docs/5_룰/5-9_검증과_측정.md 판정 효과 프로토콜): 화면이 부르는 API 경로로 옛 경로와 새 경로를 돌려 비교표를 낸다.

eval_e2e.py 는 판정() 을 확정비목 · 답변 · org 없이 불러 화면 경로를 증명하지 못한다. 이 도구는 ux_flow_check.py 처럼 서버를 프로세스 안에서 띄우고
화면과 같은 순서로 부른다: POST /api/normalize → 첫 분류 후보 선택 → POST /api/questions → 답변 · 숫자 칸 → POST /api/judge.
값을 고르는 규칙은 프런트(app/checkumait-app.tsx · lib/questions.ts)와 같다. 옛 경로 = SUDDOE_ONTOLOGY_PROGRAMS 비움, 새 경로 = 사업 지정.

입력(문항)
  --입력 대화(기본): 첫질문 + 각본 답. 정답 라벨이 전체 사실 기준이라 사실을 다 말한 사용자로 돈다. 첫질문: 짧은 질문만.
  --답변 충족(기본): 확인 질문을 전부 요건을 채우는 쪽으로 답한다(틀린 '가능' 이 가장 잘 드러나는 쪽). 모름: 전부 모르겠어요(코드가 '가능' 을 내리는 쪽).
  숫자 칸은 질의 변환 수치로 미리 채운 값을 그대로 보낸다(화면 기본값). 정답 판정 · 정답 근거 · 정답 비목은 입력에 쓰지 않는다. 채점에만 쓴다.
  L3 문항은 데모 세션으로 임시 기관을 만들고 기관규정문을 /api/l3/upload 로 올린 뒤 그 토큰으로 돈다. 끝나면 지운다.
  L3 아닌 문항은 비회원(org 없음)으로 돈다. L3 가 없는 기관과 문맥이 같고 tenant 에 아무것도 쓰지 않는다.

쓰지 않는 것: 판정 로그(기록=False), 계획(plan_id 없음). 응답 캐시는 읽지 않는다(회차 · 경로마다 실제로 판정한다). 질문세트 저장만 프로세스 메모리에 둔다.

채점(결정론, LLM 없음)
  판정 일치, 판정+인용(응답 인용 중 결정 근거와 같은 조가 1개 이상, golden_v2/README.md "정확도 정의"), 틀린 '가능'(정답이 가능이 아닌데 가능).
  호출 실패(HTTP 오류, 정규화 오류, 판정 파이프라인 실패 경로, 시간 초과)와 분류 없음(사용자가 직접 골라야 하는 문항)은 틀린 답과 따로 세고 분모에서 뺀다.
  코드 판정(게이트 A · 코드)과 LLM 판정은 따로 센다. 문항 본문과 정답 값은 출력하지 않는다.

실행:
  본 측정  PYTHONIOENCODING=utf-8 python scripts/eval_screen_path.py --세트 v2_dev --사업 "모두의 창업 프로젝트" --회차 2 --기록
  연결만   PYTHONIOENCODING=utf-8 python scripts/eval_screen_path.py --세트 v2_dev --사업 "모두의 창업 프로젝트" --dry --limit 5
  다시 표  PYTHONIOENCODING=utf-8 python scripts/eval_screen_path.py --비교 210,211,212,213
환경변수: UX_CHECK_APP_DSN · UX_CHECK_OWNER_DSN(ux_flow_check.py 와 같다. 기본 로컬 DB). 실제 LLM 은 SUDDOE_LLM_* · SUDDOE_QWEN_MODEL.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ux_flow_check as ux                                            # noqa: E402

_L3_문서 = "L3_기관규정문"     # 정답 근거의 가상 문서. golden_v2_load._L3_문서 와 같다
_경로값 = {"옛": "", "새": None}   # None 은 --사업 값
_실패_상태 = "호출실패"
_분류없음_상태 = "분류없음"
_코드_게이트 = ("A", "코드")


@dataclass
class 문항:
    gold_id: int
    질문: str
    사업명: str
    정답: str
    결정근거: list[dict]
    L3: bool
    답들: list[str]
    기관규정문: str | None


# ── 문항 ─────────────────────────────────────────────────────────────

def 문항_읽기(세트: str, 사업: str, L3: str, limit: int | None) -> list[문항]:
    """eval.golden_set 을 읽는다. 정답 비목 칸은 읽지 않는다(판정 입력이 되지 않게)."""
    import psycopg
    import eval_store
    조건, 인자 = "세트 = %s AND 사업명 = %s", [세트, 사업]
    if L3 != "전체":
        조건 += " AND coalesce((문항메타->>'L3')::boolean, false) = %s"
        인자.append(L3 == "만")
    with psycopg.connect(ux.OWNER_DSN, connect_timeout=5, autocommit=True) as c:
        행들 = c.execute(f"""SELECT gold_id, 질문, 사업명, 정답판정, 정답근거, 문항메타 FROM eval.golden_set
                             WHERE {조건} ORDER BY gold_id""", 인자).fetchall()
    if not 행들:
        sys.exit(f"세트 {세트!r} · 사업 {사업!r} 문항이 없다. scripts/golden_v2_load.py 를 먼저 실행한다.")
    if any("답들" not in (r[5] or {}) for r in 행들):
        sys.exit("문항메타에 답들 · 기관규정문이 없다. scripts/golden_v2_load.py 를 다시 실행한다.")
    문항들 = [문항(r[0], r[1], r[2], r[3], [g for g in r[4] or [] if "결정" in (g.get("역할") or "")],
                bool(r[5].get("L3")), r[5].get("답들") or [], r[5].get("기관규정문")) for r in 행들]
    return eval_store.고르게_뽑기(문항들, limit, lambda m: m.L3)


def 입력_글(m: 문항, 입력: str) -> str:
    return " ".join([m.질문, *m.답들]) if 입력 == "대화" else m.질문


# ── 화면 흐름 한 문항 ────────────────────────────────────────────────

def _임시기관(client, m: 문항, 만든기관: list[str], 잠금: threading.Lock) -> tuple[dict, str, str]:
    """L3 문항: 데모 세션 + 기관규정문 업로드 → (헤더, org, 응답 인용에 나올 문서 이름). 실패는 RuntimeError(호출 실패로 센다)."""
    import jwt

    from llm_validate import _l3표시명
    r = client.post("/api/demo/session")
    if r.status_code != 200:
        raise RuntimeError(f"데모 세션 {r.status_code}")
    토큰 = r.json()["access_token"]
    org = jwt.decode(토큰, options={"verify_signature": False})["org"]
    with 잠금:
        만든기관.append(org)
    헤더 = {"Authorization": f"Bearer {토큰}"}
    파일명 = f"평가기관_{m.gold_id}_사업비_규정.hwpx"
    r = client.post("/api/l3/upload", headers=헤더,
                    files={"파일": (파일명, ux._hwpx(["평가기관 사업비 집행 규정", m.기관규정문 or ""]), "application/octet-stream")})
    doc = r.json().get("doc_id") if r.status_code == 202 else None
    상태 = client.get(f"/api/l3/{doc}", headers=헤더).json() if doc else {}
    if 상태.get("상태") != "완료":
        raise RuntimeError(f"L3 업로드 {r.status_code} · 상태 {상태.get('상태')} · 실패사유 {상태.get('실패사유')}")
    return 헤더, org, _l3표시명(파일명)     # 응답 인용의 doc_id 는 UUID 가 아니라 이 이름이다(llm_validate._메타_행)


def _칸_값(칸들: list[dict], 수치: dict | None) -> dict:
    """lib/questions.ts 추가입력초기값 + 추가입력값만들기: 수치에 키가 있고 형식이 맞는 칸만 보낸다. 나머지 칸은 비운다."""
    값들: dict = {}
    for 칸 in 칸들:
        v = (수치 or {}).get(칸.get("키"))
        if not isinstance(v, (str, int, float)) or isinstance(v, bool):
            continue
        글 = str(v).strip()
        if 칸.get("형식") == "선택":
            if 글 in (칸.get("선택지") or []):
                값들[칸["키"]] = 글
        else:
            try:
                수 = float(글)
            except ValueError:
                continue
            if math.isfinite(수):
                값들[칸["키"]] = int(수) if 수 == int(수) else 수
    return 값들


def _저장값_읽기(질문세트: str, org: str | None) -> dict | None:
    """서버에 저장한 질문(충족답 포함). 화면은 충족답을 모르므로 답을 시뮬레이션하는 이 도구만 읽는다."""
    from server import _common, response_cache
    if org:
        with _common.org_고정(org):
            return response_cache.질문세트_조회(질문세트, org, None)
    return response_cache.질문세트_조회(질문세트, None, None)


def _답변_만들기(저장값: dict | None, 답변: str) -> list[dict]:
    """--답변 충족: 충족답 그대로(충족답이 없는 코드 질문은 모름). 모름: 전부 모름."""
    out = []
    for q in (저장값 or {}).get("질문") or []:
        out.append({"code": q["id"], "값": (q.get("충족답") or "모름") if 답변 == "충족" else "모름"})
    return out


def 화면_흐름(client, m: 문항, a, 만든기관: list[str], 잠금: threading.Lock, 생_판정: dict) -> dict:
    """한 문항을 화면 순서대로 돈다. 반환: {상태, 사유, 결과, 원본, 질문수, 칸수, 답변수, 칸값, 분류, l3_문서, 캐시}."""
    from server.catalog import _사업_비목
    t0 = time.time()
    흐름: dict = {"상태": _실패_상태, "사유": None, "결과": None, "원본": None, "질문수": 0, "칸수": 0, "칸값수": 0,
                "답변수": 0, "분류": None, "l3_문서": None, "캐시": False}
    헤더, org = {}, None
    if m.L3:
        헤더, org, 흐름["l3_문서"] = _임시기관(client, m, 만든기관, 잠금)

    r = client.post("/api/normalize", headers=헤더, json={"질문": 입력_글(m, a.입력), "사업명": m.사업명})
    정규화 = ux._sse_이벤트(r.text, "결과") if r.status_code == 200 else None
    if not isinstance(정규화, dict):
        흐름["사유"] = f"정규화 {r.status_code} {(ux._sse_이벤트(r.text, '오류') or {}).get('종류') if r.status_code == 200 else ''}".strip()
        return 흐름

    선택지 = _사업_비목(m.사업명)
    첫분류 = next((c for c in 정규화.get("분류후보") or [] if c.get("비목") in 선택지), None)
    비목 = (첫분류 or {}).get("비목") or next((c.get("비목") for c in 정규화.get("비목후보") or [] if c.get("비목") in 선택지), None)
    세목 = (첫분류 or {}).get("세목")
    if not 비목 and a.dry:          # LLM 없는 연결 확인 전용. 규칙으로 찾은 비목, 없으면 재료비. 판정이 없어 결과에 영향이 없다
        import normalize_run
        비목 = next((b for b in normalize_run.규칙_정규화(입력_글(m, a.입력))["비목후보"] if b in 선택지), "재료비")
    if not 비목:                     # 화면은 여기서 사용자가 직접 고른다. 이 도구는 대신 고르지 않는다
        흐름.update(상태=_분류없음_상태, 사유="분류 후보 없음")
        return 흐름
    흐름["분류"] = {"비목": 비목, "세목": 세목}

    수치 = 정규화.get("수치") if isinstance(정규화.get("수치"), dict) else None
    부가세 = (수치 or {}).get("부가세구분") if (수치 or {}).get("부가세구분") in ("포함", "공급가액") else "모름"
    r = client.post("/api/questions", headers=헤더, json={
        "사업명": m.사업명, "확정비목": 비목, "확정세목": 세목, "부가세구분": 부가세,
        "품목": 정규화.get("품목"), "금액": 정규화.get("금액"), "용도": 정규화.get("용도"),
        "질문원문": (정규화.get("질문원문") or "")[:2000] or None})
    응답 = r.json() if r.status_code == 200 else {}
    # lib/questions.ts 질문요청: 상태가 있음이 아니면 질문 없이 바로 판정한다(오류가 아니다)
    있음 = 응답.get("상태") == "있음"
    질문들 = (응답.get("질문") or []) if 있음 and 응답.get("질문세트") else []
    칸들 = (응답.get("추가입력") or []) if 있음 else []
    칸값 = _칸_값(칸들, 수치)
    답변 = _답변_만들기(_저장값_읽기(응답["질문세트"], org), a.답변) if 질문들 else []
    흐름.update(질문수=len(질문들), 칸수=len(칸들), 칸값수=len(칸값), 답변수=len(답변), 질문상태=응답.get("상태"))

    # checkumait-app.tsx 저장할정규화: 수치에서 칸 키를 지우고 칸 값 · 부가세를 넣고 세목을 적는다. 옛 경로(수치 없음)는 그대로 보낸다
    보낼정규화 = dict(정규화, _평가키=f"{m.gold_id}")
    if 수치 is not None:
        보낼정규화["수치"] = {**{k: v for k, v in 수치.items() if k not in {c.get("키") for c in 칸들}}, **칸값, "부가세구분": 부가세}
        if 세목:
            보낼정규화["확정세목"] = 세목
    r = client.post("/api/judge", headers=헤더, json={
        "사업명": m.사업명, "확정비목": 비목, "정규화": 보낼정규화, "답변": 답변,
        "질문세트": 응답.get("질문세트") if 질문들 else None, "추가입력값": 칸값, "확정세목": 세목, "부가세구분": 부가세})
    결과 = ux._sse_이벤트(r.text, "결과") if r.status_code == 200 else None
    원본 = 생_판정.pop(f"{m.gold_id}", None)
    흐름.update(결과=결과, 원본=원본, 캐시=bool((ux._sse_이벤트(r.text, "완료") or {}).get("캐시")),
              지연ms=int((time.time() - t0) * 1000))
    if not isinstance(결과, dict):
        흐름["사유"] = f"판정 {r.status_code}"
    elif 원본 is None:                # 라우트 시간 초과 · 예외. 파이프라인 결과가 없다
        흐름["사유"] = "판정 라우트 실패(시간 초과 또는 예외)"
    elif 원본.get("실패경로"):
        흐름["사유"] = f"파이프라인 실패 {원본.get('실패단계')}"
    else:
        흐름["상태"] = "판정"
    return 흐름


# ── 채점 ─────────────────────────────────────────────────────────────

def 인용_일치(인용들: list[dict], 결정근거: list[dict], l3_문서: str | None) -> bool:
    """응답 인용 중 결정 근거와 같은 조가 1개 이상인가. 가상 L3 근거는 업로드한 문서 인용과 맞춘다(문항마다 기관규정문 한 건이라 조까지 보지 않는다)."""
    from llm_validate import _조번호_표시
    표 = {(c.get("doc_id"), c.get("조번호")) for c in 인용들 or []}
    문서들 = {c.get("doc_id") for c in 인용들 or []}
    for g in 결정근거:
        if g.get("doc_id") == _L3_문서:
            if l3_문서 and l3_문서 in 문서들:
                return True
        elif (g.get("doc_id"), g.get("조번호")) in 표 or (g.get("doc_id"), _조번호_표시(g.get("조번호"))) in 표:
            return True
    return False


def 채점(m: 문항, 흐름: dict, 경로: str) -> dict:
    """문항 하나 → run_item. 호출 실패 · 분류 없음은 적중 NULL 이다(틀린 답과 구분한다)."""
    원 = 흐름.get("원본") or {}
    공통 = {"상태": 흐름["상태"], "사유": 흐름.get("사유"), "경로조건": 경로, "L3": m.L3, "분류": 흐름.get("분류"),
          "질문수": 흐름.get("질문수"), "칸수": 흐름.get("칸수"), "칸값수": 흐름.get("칸값수"), "답변수": 흐름.get("답변수"),
          "캐시": 흐름.get("캐시"), "지연ms": 흐름.get("지연ms"), "실제경로": 원.get("경로") or ("옛" if 원 else None),
          "게이트": 원.get("게이트"), "처리단계": 원.get("처리단계"), "검증코드": 원.get("검증코드"),
          "코드판정": bool(원.get("코드판정") or 원.get("게이트") in _코드_게이트)}
    if 원.get("dry"):
        공통.update(프롬프트길이=원.get("프롬프트길이"), 문장맵=원.get("문장맵_크기"), b1=원.get("b1"),
                  문맥요약=원.get("문맥요약"), 근거경로수=len(원.get("근거경로") or []))
        return {"gold_id": m.gold_id, "예측": "dry", "정답": m.정답, "적중": None, "원출력": 공통}
    if 흐름["상태"] != "판정":
        return {"gold_id": m.gold_id, "예측": 흐름["상태"], "정답": m.정답, "적중": None, "원출력": 공통}
    결과 = 흐름["결과"]
    인용들 = 결과.get("인용") or []
    예측 = 결과.get("판정")
    공통.update(판정일치=예측 == m.정답, 인용일치=인용_일치(인용들, m.결정근거, 흐름.get("l3_문서")),
              인용조=sorted({f'{c.get("doc_id")}#{c.get("조번호")}' for c in 인용들}),
              근거경로=결과.get("근거경로"), 조건수=len(결과.get("조건") or []), 신뢰등급=결과.get("신뢰등급"))
    return {"gold_id": m.gold_id, "예측": 예측, "정답": m.정답, "적중": 공통["판정일치"] and 공통["인용일치"], "원출력": 공통}


def 지표(항목들: list[dict]) -> dict:
    """run_item 목록 → 지표. 분모 = 판정이 나온 문항(판단불가 포함). 항목만 읽으므로 --비교 에서 DB 값으로 다시 계산한다."""
    판정들 = [x for x in 항목들 if x["원출력"].get("상태") == "판정"]
    틀린 = [x for x in 판정들 if x["예측"] == "가능" and x["정답"] != "가능"]
    코드 = [x for x in 판정들 if x["원출력"].get("코드판정")]
    llm = [x for x in 판정들 if not x["원출력"].get("코드판정")]
    지연 = sorted(x["원출력"].get("지연ms") or 0 for x in 판정들)

    def 일치(묶음):
        return sum(1 for x in 묶음 if x["원출력"].get("판정일치"))
    return {"문항": len(항목들), "분모": len(판정들),
            "호출실패": sum(1 for x in 항목들 if x["원출력"].get("상태") == _실패_상태),
            "분류없음": sum(1 for x in 항목들 if x["원출력"].get("상태") == _분류없음_상태),
            "실패사유": dict(Counter(x["원출력"].get("사유") for x in 항목들 if x["원출력"].get("상태") == _실패_상태)),
            "판정일치": 일치(판정들), "판정인용": sum(1 for x in 판정들 if x["적중"]),
            "틀린가능": len(틀린), "틀린가능_불가조건부": sum(1 for x in 틀린 if x["정답"] in ("불가", "조건부")),
            "틀린가능_문항": sorted(x["gold_id"] for x in 틀린),
            "판단불가": sum(1 for x in 판정들 if x["예측"] == "판단불가"),
            "판정분포": dict(Counter(x["예측"] for x in 판정들)),
            "코드판정": len(코드), "코드판정_일치": 일치(코드),
            "코드판정_틀림_문항": sorted(x["gold_id"] for x in 코드 if not x["원출력"].get("판정일치")),
            "LLM판정": len(llm), "LLM판정_일치": 일치(llm),
            "경로불일치": sum(1 for x in 판정들 if (x["원출력"].get("실제경로") == "온톨로지") != (x["원출력"].get("경로조건") == "새")),
            "캐시적중": sum(1 for x in 항목들 if x["원출력"].get("캐시")),
            "지연p95ms": 지연[max(0, math.ceil(len(지연) * 0.95) - 1)] if 지연 else None}


def 비교표(묶음들: list[tuple[str, int, dict, list[dict]]]) -> str:
    """(경로, 회차, 지표, 항목들) 목록 → 표. 틀린 '가능' 증가 기준은 새 경로 최댓값 - 옛 경로 최솟값이다(비결정성에서 나쁜 쪽)."""
    줄 = [f"{'경로':4} {'회차':>2} {'문항':>4} {'분모':>4} {'실패':>4} {'분류없음':>6} {'틀린가능':>6} {'판정일치':>6} {'판정+인용':>7} "
         f"{'판단불가':>6} {'코드판정':>6} {'코드일치':>6} {'p95초':>6}"]
    for 경로, 회차, g, _ in 묶음들:
        p95 = f"{g['지연p95ms'] / 1000:.0f}" if g["지연p95ms"] is not None else "-"
        줄.append(f"{경로:4} {회차:>2} {g['문항']:>4} {g['분모']:>4} {g['호출실패']:>4} {g['분류없음']:>6} {g['틀린가능']:>6} {g['판정일치']:>6} "
                 f"{g['판정인용']:>7} {g['판단불가']:>6} {g['코드판정']:>6} {g['코드판정_일치']:>6} {p95:>6}")
    옛 = [g for 경로, _, g, _ in 묶음들 if 경로 == "옛"]
    새 = [g for 경로, _, g, _ in 묶음들 if 경로 == "새"]

    def 범위(묶음, 키):
        값 = [g[키] for g in 묶음]
        return f"{min(값)}~{max(값)}" if 값 else "-"
    줄.append("")
    for 키, 이름 in (("틀린가능", "틀린 '가능'"), ("판정일치", "판정 일치"), ("판정인용", "판정+인용"), ("호출실패", "호출 실패")):
        줄.append(f"범위 {이름}: 옛 {범위(옛, 키)} · 새 {범위(새, 키)}")
    if 옛 and 새:
        증가 = max(g["틀린가능"] for g in 새) - min(g["틀린가능"] for g in 옛)
        옛_틀림 = {i for g in 옛 for i in g["틀린가능_문항"]}
        새로_틀림 = sorted({i for g in 새 for i in g["틀린가능_문항"]} - 옛_틀림)
        줄.append(f"틀린 '가능' 증가(새 최대 - 옛 최소): {증가} → 기준 0 {'통과' if 증가 <= 0 else '미달'}")
        줄.append(f"새 경로에서만 틀린 '가능' 이 난 문항 {len(새로_틀림)}개: {새로_틀림}")
        if min(g["분모"] for g in 새) < max(g["분모"] for g in 옛):
            줄.append("경고: 새 경로 분모가 옛 경로보다 작다(호출 실패 · 분류 없음). 틀린 '가능' 증가가 실제보다 작게 보일 수 있으니 빠진 문항과 함께 읽는다.")
    코드틀림 = sorted({i for 경로, _, g, _ in 묶음들 if 경로 == "새" for i in g["코드판정_틀림_문항"]})
    줄.append(f"새 경로 코드 판정이 정답과 다른 문항 {len(코드틀림)}개(규범 확인 대상): {코드틀림}")
    실패 = Counter()
    for _, _, g, _ in 묶음들:
        실패.update(g["실패사유"])
    if 실패:
        줄.append(f"호출 실패 사유: {dict(실패)}")
    if any(g["경로불일치"] or g["캐시적중"] for _, _, g, _ in 묶음들):
        줄.append(f"경고: 경로 불일치 {[g['경로불일치'] for _, _, g, _ in 묶음들]} · 캐시 적중 {[g['캐시적중'] for _, _, g, _ in 묶음들]}")
    return "\n".join(줄)


def 차이_요약(묶음들: list[tuple[str, int, dict, list[dict]]]) -> str:
    """같은 회차의 옛 · 새 판정이 다른 문항 수. 설정을 바꾸면 결과가 바뀌는지 본다. 문항 본문은 내지 않는다."""
    회차들: dict[int, dict[str, dict[int, dict]]] = {}
    for 경로, 회차, _, 항목들 in 묶음들:
        회차들.setdefault(회차, {})[경로] = {x["gold_id"]: x for x in 항목들}
    줄 = []
    for 회차, 양쪽 in sorted(회차들.items()):
        if {"옛", "새"} - set(양쪽):
            continue
        공통 = set(양쪽["옛"]) & set(양쪽["새"])
        다름 = sorted(i for i in 공통 if 양쪽["옛"][i]["예측"] != 양쪽["새"][i]["예측"])
        인용다름 = sorted(i for i in 공통 if 양쪽["옛"][i]["원출력"].get("인용조") != 양쪽["새"][i]["원출력"].get("인용조"))
        줄.append(f"{회차}회차 옛 ≠ 새: 판정 {len(다름)}/{len(공통)}문항 {다름} · 인용 조 {len(인용다름)}/{len(공통)}문항 {인용다름}")
    return "\n".join(줄)


# ── 실행 ─────────────────────────────────────────────────────────────

def 환경_준비(l3_dir: str) -> None:
    """ux_flow_check 와 같은 live 환경 + 측정용 요청 한도. 서버 모듈을 import 하기 전에 부른다."""
    ux._환경_준비(l3_dir)
    os.environ["SUDDOE_IP_HOURLY_LIMIT"] = "1000000"
    os.environ["SUDDOE_DAILY_JUDGE_LIMIT"] = "1000000"


def 서버_준비(a) -> dict:
    """판정 계측을 건다. 판정 로그를 남기지 않고, dry 면 LLM 을 막는다. 응답 캐시는 읽지 않는다. 반환: 파이프라인 결과 보관함."""
    import normalize_run
    import orchestrate
    import question_generate
    from server import response_cache
    생_판정: dict = {}
    원래_판정 = orchestrate.판정

    def 판정_계측(질문, **k):
        키 = (k.get("정규화결과") or {}).pop("_평가키", None)
        k["기록"] = False
        if a.dry:
            k["dry"] = True
        r = 원래_판정(질문, **k)
        if 키:
            생_판정[키] = r
        return r

    orchestrate.판정 = 판정_계측
    response_cache.조회 = lambda *_a, **_k: None
    if a.dry:
        def llm_금지(*_a, **_k):
            raise orchestrate.LLM실패("--dry 는 LLM 을 호출하지 않는다")
        원래_변환, 원래_정규화 = normalize_run.질의변환, normalize_run.정규화
        normalize_run.질의변환 = lambda 입력, 레이블, **k: 원래_변환(입력, 레이블, dry=True)
        normalize_run.정규화 = lambda 질문, **k: 원래_정규화(질문, dry=True)
        question_generate.llm_호출 = llm_금지
    return 생_판정


def 조건_실행(경로: str, 회차: int, 문항들: list[문항], a, 생_판정: dict) -> list[dict]:
    """한 조건(경로 · 회차)을 끝까지 돈다. 경로 환경변수는 프로세스 전체 값이라 조건은 순서대로만 돈다. 만든 임시 기관은 지운다."""
    from fastapi.testclient import TestClient

    from server import response_cache
    from server.main import app
    os.environ["SUDDOE_ONTOLOGY_PROGRAMS"] = a.사업 if 경로 == "새" else ""
    response_cache.데이터버전_해시_무효화()     # 해시에 환경변수가 들어간다. TTL 캐시가 옛 값을 내지 않게 한다
    response_cache._메모리.clear()             # 같은 입력의 질문세트를 다른 경로 · 회차에서 다시 쓰지 않는다
    만든기관: list[str] = []
    잠금 = threading.Lock()
    지역 = threading.local()

    def 하나(m: 문항) -> dict:
        client = getattr(지역, "client", None) or TestClient(app, raise_server_exceptions=False)
        지역.client = client
        try:
            흐름 = 화면_흐름(client, m, a, 만든기관, 잠금, 생_판정)
        except Exception as e:                                       # noqa: BLE001
            흐름 = {"상태": _실패_상태, "사유": f"{type(e).__name__}: {str(e)[:120]}"}
        항목 = 채점(m, 흐름, 경로)
        o = 항목["원출력"]
        print(f"  {경로} {회차}회 gold_id={m.gold_id:<6} {항목['예측']:<6} 일치={o.get('판정일치')} 인용={o.get('인용일치')} "
              f"게이트={o.get('게이트')} 실제경로={o.get('실제경로')} 질문={o.get('질문수')} 칸={o.get('칸값수')}/{o.get('칸수')} "
              f"{(o.get('지연ms') or 0) // 1000}초 {o.get('사유') or ''}"
              + (f" 프롬프트={o.get('프롬프트길이')} 문장맵={o.get('문장맵')} 문맥={o.get('문맥요약')} L3={o.get('b1')}" if 항목["예측"] == "dry" else ""),
              flush=True)
        return 항목
    try:
        with ThreadPoolExecutor(max_workers=a.동시) as ex:
            return list(ex.map(하나, 문항들))
    finally:
        if 만든기관:
            ux._정리(만든기관)


def 조건_기록(경로: str, 회차: int, 항목들: list[dict], g: dict, a, 묶음키: str) -> int:
    import psycopg

    import eval_store
    from assemble_context import 프롬프트_버전
    with psycopg.connect(ux.OWNER_DSN, connect_timeout=5) as c:
        return eval_store.기록(
            {"종류": "judge",
             "설정": {"측정": "화면 경로 판정 효과", "경로": 경로, "회차": 회차, "묶음": 묶음키, "세트": a.세트, "사업": a.사업, "L3": a.L3,
                      "입력": a.입력, "답변": a.답변, "동시": a.동시, "프롬프트버전": 프롬프트_버전,
                      "SUDDOE_ONTOLOGY_PROGRAMS": a.사업 if 경로 == "새" else "",
                      "모델": os.environ.get("SUDDOE_QWEN_MODEL"), "폴백모델": os.environ.get("SUDDOE_QWEN_FALLBACK_MODEL"),
                      "질문모델": os.environ.get("SUDDOE_QUESTION_MODEL"), "질의변환모델": os.environ.get("SUDDOE_QUERY_MODEL")},
             "문항수": len(항목들), "지표": g,
             "라벨": f"화면경로 {경로} {회차}회 세트={a.세트} 사업={a.사업} L3={a.L3} 입력={a.입력} 답변={a.답변}",
             "비고": (f"조건: {경로} 경로 · 세트={a.세트} · 사업={a.사업} · {g['문항']}문항(분모 {g['분모']}, 호출 실패 {g['호출실패']}, 분류 없음 {g['분류없음']}) · "
                      f"입력={a.입력} · 답변={a.답변} · 라벨은 AI, 사람 미검수.")},
            항목들, conn=c)


def 비교_다시(run_ids: list[int]) -> None:
    """저장된 run 들로 표를 다시 낸다(결정론 재계산)."""
    import psycopg
    묶음들, 조건들 = [], set()
    with psycopg.connect(ux.OWNER_DSN, connect_timeout=5, autocommit=True) as c:
        for run_id in run_ids:
            행 = c.execute("SELECT 설정, 라벨 FROM eval.runs WHERE run_id = %s", (run_id,)).fetchone()
            if not 행 or 행[0].get("측정") != "화면 경로 판정 효과":
                sys.exit(f"run {run_id} 은 이 도구의 기록이 아니다")
            조건들.add(tuple(행[0].get(k) for k in ("세트", "사업", "L3", "입력", "답변")))
            항목들 = [{"gold_id": r[0], "예측": r[1], "정답": r[2], "적중": r[3], "원출력": r[4]} for r in c.execute(
                "SELECT gold_id, 예측, 정답, 적중, 원출력 FROM eval.run_items WHERE run_id = %s", (run_id,)).fetchall()]
            print(f"run {run_id}: {행[1]}")
            묶음들.append((행[0]["경로"], 행[0]["회차"], 지표(항목들), 항목들))
    if len(조건들) > 1:
        print(f"경고: 조건이 다른 run 이 섞였다(세트 · 사업 · L3 · 입력 · 답변): {sorted(조건들, key=str)}")
    print("\n" + 비교표(묶음들) + "\n" + 차이_요약(묶음들))


def main() -> None:
    if (sys.stdout.encoding or "").lower().replace("-", "") != "utf8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--세트", choices=("v2_dev", "v2_test"), default="v2_dev")
    ap.add_argument("--사업", default="모두의 창업 프로젝트")
    ap.add_argument("--L3", choices=("전체", "제외", "만"), default="전체", help="L3 문항은 임시 기관을 만들고 지운다")
    ap.add_argument("--경로", default="옛,새", help="돌릴 경로. 옛,새 · 옛 · 새")
    ap.add_argument("--회차", type=int, default=2, help="경로마다 반복 횟수(판정 비결정성, 5-9 는 2회 이상)")
    ap.add_argument("--입력", choices=("대화", "첫질문"), default="대화")
    ap.add_argument("--답변", choices=("충족", "모름"), default="충족")
    ap.add_argument("--limit", type=int, default=None, help="소량 실행. L3 비율을 맞춰 고르게 뽑는다")
    ap.add_argument("--동시", type=int, default=4, help="경로 안에서 동시에 도는 문항 수")
    ap.add_argument("--dry", action="store_true", help="LLM 없이 프롬프트 구성까지만. 비목은 규칙값이라 판정 품질을 재지 않는다. 기록하지 않는다")
    ap.add_argument("--기록", action="store_true", help="조건마다 eval.runs 에 종류='judge' 로 남긴다")
    ap.add_argument("--비교", default=None, help="저장된 run_id 들(쉼표)로 표만 다시 낸다. LLM · 서버 없음")
    a = ap.parse_args()
    if a.비교:
        비교_다시([int(x) for x in a.비교.split(",")])
        return
    경로들 = [x.strip() for x in a.경로.split(",") if x.strip()]
    if not 경로들 or set(경로들) - set(_경로값):
        ap.error("--경로 는 옛,새 중에서 고른다")
    사유 = ux._db_접속가능()
    if 사유:
        sys.exit(사유)
    문항들 = 문항_읽기(a.세트, a.사업, a.L3, a.limit)
    print(f"세트 {a.세트} · 사업 {a.사업} · {len(문항들)}문항(L3 {sum(1 for m in 문항들 if m.L3)}) · 입력={a.입력} · 답변={a.답변} · "
          f"경로 {경로들} · {a.회차}회 · dry={a.dry}", flush=True)
    with tempfile.TemporaryDirectory(prefix="eval_screen_l3_") as d:
        환경_준비(d)
        import retrieve
        retrieve.워밍업()
        생_판정 = 서버_준비(a)
        묶음키 = time.strftime("%Y%m%d-%H%M%S")
        묶음들 = []
        for 회차 in range(1, a.회차 + 1):
            for 경로 in 경로들:
                항목들 = 조건_실행(경로, 회차, 문항들, a, 생_판정)
                g = 지표(항목들)
                묶음들.append((경로, 회차, g, 항목들))
                if a.기록 and not a.dry:
                    print(f"[기록] eval.runs run_id={조건_기록(경로, 회차, 항목들, g, a, 묶음키)} ({경로} {회차}회)", flush=True)
    if a.dry:
        print("\n--dry: 판정이 없어 채점 · 비교표를 내지 않는다. 위 줄의 실제경로 · 프롬프트 · 문장맵이 경로 설정으로 달라지는지 본다.")
        return
    print("\n" + 비교표(묶음들) + "\n" + 차이_요약(묶음들))


if __name__ == "__main__":
    main()
