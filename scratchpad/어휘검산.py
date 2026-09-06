# -*- coding: utf-8 -*-
"""어휘 제안(별칭) 검산 — 룰검산.py 와 «스키마가 다르다». 그래서 별도 파일이다.

사용: PYTHONIOENCODING=utf-8 python scratchpad/어휘검산.py scratchpad/어휘제안_어C.json
입력 한 건:
  {"상품명":"사무실임차료","비목":"지급수수료","사업명":"재도전성공패키지",
   "근거":{"doc_id":"...","조번호":"제18조"},
   "원문발췌":"<그 조 본문에 문자 그대로 있는 문장>"}

🔴 이 도구에 ✅ 는 없다. 통과는 「거짓이라고 증명되지 않았다」일 뿐이다.
   «이 별칭이 옳은가» 는 기계가 못 본다 — 사람이 본다.
"""
import sys, os, json, re

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "_lib"))
import db  # noqa: E402

정본비목 = ("재료비", "외주용역비", "기계장치", "특허권등무형자산취득비", "인건비",
            "지급수수료", "여비", "교육훈련비", "광고선전비", "창업활동비")
상식어 = ("맥북", "노트북", "스마트폰", "책상", "의자", "항공권", "호텔", "KTX", "월급", "급여")


def 납작(s):
    return re.sub(r"\s+", "", s or "")


def 검사(제안, cur):
    결과 = []
    for i, x in enumerate(제안):
        문제, 유보 = [], []
        상품 = x.get("상품명")
        비목 = x.get("비목")
        사업 = x.get("사업명")
        근 = x.get("근거") or {}
        발췌 = x.get("원문발췌") or ""

        if not 상품:
            문제.append("상품명이 없다")
        if not 비목:
            문제.append("비목이 없다")
        if not 발췌:
            문제.append("원문발췌가 없다")
        if not (근.get("doc_id") and 근.get("조번호")):
            문제.append("근거에 doc_id/조번호가 없다")

        # 비목이 정본 10종인가 — TIPS 는 계통이 달라 유보로 돌린다
        if 비목 and 비목 not in 정본비목:
            (유보 if (사업 or "").upper().find("TIPS") >= 0 or "팁스" in (사업 or "")
             else 문제).append(f"비목 '{비목}' 이 정본 10종이 아니다")

        # 상식 별칭 차단 — LLM 이 이미 안다(실측 65.1%). 넣으면 관리 부채만 는다
        if 상품 and any(k in 상품 for k in 상식어):
            문제.append(f"상식 별칭으로 보인다('{상품}') — 규정 고유만 넣는다")

        # 🔴 핵심: 원문발췌가 그 조 본문에 «문자 그대로» 있는가
        본문 = None
        if 근.get("doc_id") and 근.get("조번호"):
            cur.execute(
                "select a.본문 from corpus.doc_articles a join corpus.documents d using(doc_id) "
                "where d.status=%s and d.doc_id=%s and a.조번호=%s and not a.삭제",
                ("active", 근["doc_id"], 근["조번호"]))
            r = cur.fetchone()
            if not r:
                문제.append(f"현행 문서에 그 조가 없다 ({근['doc_id'][:30]} {근['조번호']})")
            else:
                본문 = r[0]

        if 본문 is not None and 발췌:
            if 발췌 in 본문:
                pass
            elif 납작(발췌) in 납작(본문):
                유보.append("공백만 다르다 — 원문 그대로 옮겨라")
            else:
                문제.append("🔴 원문발췌가 그 조 본문에 «없다»")

        # 상품명이 발췌 안에 실제로 등장하는가 (엉뚱한 문장을 근거로 붙이는 것 차단)
        if 발췌 and 상품 and 납작(상품) not in 납작(발췌):
            유보.append(f"발췌 안에 '{상품}' 이 안 보인다 — 근거 문장이 맞나")

        결과.append((i, 상품, 비목, 문제, 유보))
    return 결과


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    with open(sys.argv[1], encoding="utf-8") as f:
        제안 = json.load(f)
    if not isinstance(제안, list):
        print("🔴 최상위가 리스트여야 한다")
        return 2

    with db.connect() as c, c.cursor() as cur:
        결과 = 검사(제안, cur)

    통과 = 거부 = 확인불가 = 0
    for i, 상품, 비목, 문제, 유보 in 결과:
        if 문제:
            거부 += 1
            print(f"🔴 [{i}] {상품} -> {비목}")
            for m in 문제:
                print(f"      {m}")
        elif 유보:
            확인불가 += 1
            print(f"⚠️ [{i}] {상품} -> {비목}")
            for m in 유보:
                print(f"      {m}")
        else:
            통과 += 1
    print(f"\n통과 {통과} · 🔴거부 {거부} · ⚠️확인불가 {확인불가}   (총 {len(결과)})")
    print("🔴 «확인불가» 를 통과로 세지 마라. 그리고 이 도구에 ✅ 는 없다 —")
    print("   기계가 보는 것은 «원문에 그 문장이 있는가» 뿐이다.")
    print("   «그 조가 이 별칭의 근거로 맞는가» 는 사람이 본다.")
    return 1 if 거부 else 0


if __name__ == "__main__":
    sys.exit(main())
