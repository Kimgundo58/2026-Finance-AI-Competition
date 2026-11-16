# -*- coding: utf-8 -*-
"""① 지출 항목 입력 검증(v057) 화면 흐름 확인. 프론트와 같은 순서로 in-process API(TestClient)를 부른다.
운영 DB 읽기 전용 · LLM / 검색은 운영 서버 IP 경유 · DB 쓰기는 추적.운영보호 가 메모리로 돌린다. 게스트.

  가 「결제」 → 입력검증 지출아님 · 품목재입력 → [고쳐 쓰기] 「견적서」(입력검증_시도 2) → 설명입력
     → 한 문장 설명 → /api/questions {웹조사, 품목설명} ② 품목 조사 → [맞아요] → ③ 분류 · 질문
  나 「피그마 구독」 → 입력검증 지출 → ② 품목 조사

  PYTHONIOENCODING=utf-8 python debug/입력검증_화면흐름.py
"""
import json
import sys
from pathlib import Path

DEBUG = Path(__file__).resolve().parent
작업트리 = DEBUG.parent
for p in (str(DEBUG), str(작업트리 / "scripts"), str(작업트리)):
    sys.path.insert(0, p)

import 설정  # noqa: E402
import 운영연결  # noqa: E402
import 추적  # noqa: E402

설정.연결 = "운영"
설정.DB포트, 설정.SOCKS포트 = 15492, 11132
설정.이메일 = None
설정.호출추적, 설정.SQL기록, 설정.단계마다_멈춤 = False, False, False
설정.예산초 = 300                   # 운영 그대로
정보 = 운영연결.연결(설정)
print("운영 SHA", 정보["운영SHA"], "· 코드 SHA", 정보["코드SHA"])

기록 = 추적.기록장(설정)
추적.LLM_기록_설치(기록)
추적.운영보호(기록, 설정, None)
api = 추적.화면API(기록)

사업 = "모두의 창업 프로젝트"
결과: dict = {}


def 정리(품목, 용도, 금액, 시도=1):
    with 기록.단계(f"정리_{품목}"):
        r = api.post("/api/normalize", {"품목": 품목, "금액": 금액, "용도": 용도, "사업명": 사업, "부가세구분": "포함",
                                        **({"입력검증_시도": 시도} if 시도 > 1 else {})})
    n = dict(r.get("결과") or {})
    print("  입력검증:", n.get("입력검증"), "· 별칭일치:", (n.get("품목확인") or {}).get("별칭일치"))
    return n


def 묻기(n, **추가):
    with 기록.단계(f"질문_{n.get('품목')}"):
        r = api.post("/api/questions", {"사업명": 사업, "부가세구분": "포함", "품목": n["품목"], "금액": n["금액"],
                                         "용도": n["용도"], "질문원문": n.get("질문원문"), "조사ID": n["조사ID"],
                                         "웹조사": True, **추가})
    return r.get("완료") or {}


# 가: 결제 → 견적서 → 설명 → ② → ③
n1 = 정리("결제", "사업 진행", 50000)
결과["가_결제"] = n1.get("입력검증")
n2 = 정리("견적서", "사업 진행", 50000, 시도=2)
결과["가_견적서_시도2"] = n2.get("입력검증")
설명 = "팀원 5명이 쓰는 디자인 협업 도구 피그마 1개월 구독이에요."
q1 = 묻기(n2, 품목설명=설명)
조사 = q1.get("조사") or {}
결과["가_②"] = {"상태": q1.get("상태"), "조사상태": 조사.get("상태"), "후보": [c.get("질문") for c in 조사.get("후보") or []]}
계획 = [r for r in 기록.LLM if 설명 in json.dumps(r.get("메시지") or [], ensure_ascii=False)]
결과["가_②_설명이_든_LLM호출수"] = len(계획)
print("  ②:", 결과["가_②"], "· 설명이 든 LLM 호출", len(계획))
답 = [{"id": c["id"], "답": "맞아요"} for c in 조사.get("후보") or []]
앞 = len(기록.LLM)
q2 = 묻기(n2, 조사답변=답, 품목설명=설명)
# ③ (v056 프롬프트 변형 A · B · C + 조정): 설명(또는 설명 정리 JSON 의 「피그마」)이 분류 LLM 프롬프트에 들어갔는지
분류호출 = [r for r in 기록.LLM[앞:] if "item_classify" in (r.get("호출위치") or "")]
결과["가_③_분류LLM"] = [{"위치": r["호출위치"][-60:], "설명_있음": "피그마" in json.dumps(r.get("메시지") or [], ensure_ascii=False)}
                     for r in 분류호출]
print("  ③ 분류 LLM:", 결과["가_③_분류LLM"])
결과["가_③"] = {"상태": q2.get("상태"), "분류": q2.get("분류"), "분류안내": q2.get("분류안내"), "다음행동": q2.get("다음행동"),
              "질문수": len(q2.get("질문") or [])}
print("  ③:", 결과["가_③"])

# 나: 피그마 구독 → ②
n3 = 정리("피그마 구독", "앱 화면 디자인", 20000)
결과["나_피그마"] = n3.get("입력검증")
q3 = 묻기(n3)
결과["나_②"] = {"상태": q3.get("상태"), "조사상태": (q3.get("조사") or {}).get("상태"),
              "후보수": len((q3.get("조사") or {}).get("후보") or [])}
print("  나 ②:", 결과["나_②"])

(기록.폴더 / "입력검증_화면흐름.json").write_text(json.dumps(결과, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(결과, ensure_ascii=False, indent=2))
print("기록:", 기록.폴더)
운영연결.터널_닫기()
