# -*- coding: utf-8 -*-
"""레인 S 후속 — 유해 12건이 실제 l3룰() 결과를 바꾸나 시뮬레이션. DB 읽기 전용."""
import sys, json
sys.path.insert(0, 'scripts/_lib'); sys.path.insert(0, 'scripts')
import db, l3_load

org_id = 'cfeba091-251a-5ae4-8cc9-88c6e6679440'

# 유해 12건: (조번호, 오분류된_비목, 진짜_비목, 유형)
유해12 = [
    ("단락037", "광고선전비", "여비",          "경계뭉침"),
    ("단락060", "광고선전비", "기계장치",       "헤더근접"),
    ("단락104", "광고선전비", "교육훈련비",      "경계뭉침"),
    ("단락043", "외주용역비", "재료비",         "경계뭉침"),
    ("단락044", "외주용역비", "재료비",         "경계뭉침"),
    ("단락028", "인건비",    "공통(비목아님)",   "경계뭉침"),
    ("단락032", "외주용역비", "공통(비목아님)",   "경계뭉침"),
    ("단락106", "외주용역비", "광고선전비",      "경계뭉침"),
    ("단락108", "재료비",    "광고선전비",      "경계뭉침"),
    ("단락035", "특허권등무형자산취득비", "기계장치", "헤더근접"),
    ("단락063", "특허권등무형자산취득비", "기계장치", "헤더근접"),
    ("단락090", "특허권등무형자산취득비", "지급수수료", "헤더근접"),
]

with db.connect() as c, c.cursor() as cur:
    vocab = l3_load.비목어휘(cur)
    조문전체 = l3_load._조문들(cur, org_id)
    조문by번호 = {a["조번호"]: a for a in 조문전체}

    def 실제tag(a):
        t = l3_load.비목추정(a["조제목"], a["본문"], vocab, 제목만=True)
        if t is None:
            t = l3_load.비목추정(a["조제목"], a["본문"], vocab, 제목만=False)
        return t

    기본tag = {a["조번호"]: 실제tag(a) for a in 조문전체}

    def l3룰_시뮬(비목, tagmap):
        """l3_load.l3룰() 과 같은 순서(조번호_int NULLS LAST, 조번호)로, tagmap 을 써서 재현."""
        for a in 조문전체:  # 이미 그 순서로 로드됨 (_조문들 의 ORDER BY 그대로)
            if tagmap.get(a["조번호"]) != 비목:
                continue
            조각 = l3_load._추출(a["본문"])
            if 조각 is None:
                continue
            return a["조번호"], ("참조만" if 조각["참조만"] else 조각["허용"])
        return None, None

    def 갈래표(tagmap, 대상비목목록):
        out = {}
        for 비목 in 대상비목목록:
            out[비목] = l3룰_시뮬(비목, tagmap)
        return out

    관련비목 = sorted({m for _, mis, true, _ in 유해12 for m in (mis, true) if m != "공통(비목아님)"})

    print("=== 기준선(현재 실제 태깅) ===")
    기준 = 갈래표(기본tag, 관련비목)
    for 비목, (조, 갈래) in 기준.items():
        print(f"  {비목:24s} {갈래}  (근거 {조})")

    def 교정tag(대상_조번호_집합):
        tm = dict(기본tag)
        for 조번호, 오분류비목, 진짜비목, _ in 유해12:
            if 조번호 in 대상_조번호_집합:
                if 진짜비목 == "공통(비목아님)":
                    tm[조번호] = None  # 공통조는 어느 비목에도 안 묶는다(가장 보수적 시뮬)
                else:
                    tm[조번호] = 진짜비목
        return tm

    경계뭉침 = {c for c, m, t, y in 유해12 if y == "경계뭉침"}
    헤더근접 = {c for c, m, t, y in 유해12 if y == "헤더근접"}
    전체 = 경계뭉침 | 헤더근접

    for 라벨, 대상 in [("① 경계뭉침만 교정(9건)", 경계뭉침),
                      ("② 헤더근접만 교정(3건)", 헤더근접),
                      ("③ 둘 다 교정(12건)", 전체)]:
        print(f"\n=== {라벨} ===")
        tm = 교정tag(대상)
        결과 = 갈래표(tm, 관련비목)
        for 비목, (조, 갈래) in 결과.items():
            base조, base갈래 = 기준[비목]
            변화 = "  <<< 갈림" if 갈래 != base갈래 else ""
            print(f"  {비목:24s} {갈래}  (근거 {조}){변화}")
