# -*- coding: utf-8 -*-
"""8사업 골든셋 축분포 집계 — `_통합_축분포표.md` 의 원천.

🔴 **파일에서 직접 센다.** 다른 도구의 출력이나 워커 보고를 옮겨 적지 않는다 — 그러면 닻이 하나가 된다.
🔴 **인용 대조방식은 각 파일이 «스스로 주장한» `입력필드.대조방식` 을 안 읽는다.**
   `corpus.doc_articles` 본문과 직접 대조해 다시 잰다. 주장과 측정값은 다른 것이다.

대조방식 5단(엄격한 순서대로):
  원문그대로       정규화 없이 본문에 연속 부분문자열로 있다
  공백무시         공백·따옴표 변종만 지우면 연속 (PDF 줄바꿈이 문장 중간에 든 자리)
  페이지마커제거후 본문의 '- 17 -' 같은 페이지번호를 지워야 연속 (파싱 잔재)
  생략인용         '…' 조각이 순서대로는 있으나 연속이 아니다 → 인용으로 인정하지 않는다
  실패             조각 하나라도 없다

사용:  python 축분포_집계.py            (전체)
"""
import glob
import json
import os
import re
import sys
from collections import Counter

import psycopg

sys.path.insert(0, os.path.dirname(__file__))
from verify_golden import DSN, _PAGE, art_key, norm  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
축 = ["한도초과", "목적외", "증빙미비", "시기위반", "대상외"]


def rows_of(path):
    d = json.load(open(path, encoding="utf-8"))
    if isinstance(d, list):
        return d, "array"
    return d["문항"], "dict(" + ",".join(k for k in d if k != "문항") + ")"


def 대조5(quote, raw, nows, nopage):
    if not quote:
        return "실패"
    if "…" in quote:
        frags = [norm(f) for f in quote.split("…") if norm(f)]
        pos = 0
        for f in frags:
            i = nows.find(f, pos)
            if i < 0:
                return "실패"
            pos = i + len(f)
        return "생략인용"
    if quote in raw:
        return "원문그대로"
    q = norm(quote)
    if q in nows:
        return "공백무시"
    if q in nopage:
        return "페이지마커제거후"
    return "실패"


def main():
    with psycopg.connect(DSN) as conn:
        cur = conn.cursor()
        cur.execute("select doc_id, 조번호, 본문 from corpus.doc_articles")
        raw, nows, nopage = {}, {}, {}
        for d, a, b in cur.fetchall():
            raw[(d, a)] = b
            nows[(d, a)] = norm(b)
            nopage[(d, a)] = norm(_PAGE.sub("", b))

    총 = Counter()
    for path in sorted(glob.glob(os.path.join(BASE, "*.json"))):
        items, shape = rows_of(path)
        사업 = os.path.basename(path)[:-5]
        판정 = Counter(i["정답판정"] for i in items)
        세트 = Counter(i["세트"] for i in items)
        대상 = Counter(i["대상"] for i in items)
        범위 = Counter(i.get("평가범위") for i in items)
        비목 = Counter(i.get("비목") for i in items if i.get("비목"))
        최다 = 비목.most_common(1)[0] if 비목 else ("-", 0)
        분모 = [i for i in items if i["정답판정"] in ("불가", "조건부")]

        def 이유값(i):
            v = (i.get("입력필드") or {}).get("판정이유")
            if isinstance(v, list):          # 창업중심대학·초격차는 배열로 넣었다
                return "|".join(map(str, v)) or None
            return v

        이유 = Counter()
        for i in 분모:                        # 배열이면 축을 «따로» 센다 (분모는 문항 수 그대로)
            v = (i.get("입력필드") or {}).get("판정이유")
            for a in (v if isinstance(v, list) else [v]):
                이유[a] += 1
        이유널 = Counter(이유값(i)
                         for i in items if i["정답판정"] in ("가능", "판단불가"))
        방식 = Counter()
        for i in items:
            for e in i.get("정답근거") or []:
                k = (e.get("doc"), art_key(e.get("조번호") or ""))
                방식[대조5(e.get("원문", ""), raw.get(k, ""), nows.get(k, ""), nopage.get(k, ""))] += 1
        위반 = []
        if 판정.get("가능", 0) < 2:
            위반.append(f"가능트랩 {판정.get('가능',0)}<2")
        if len(비목) < 8:
            위반.append(f"비목 {len(비목)}종<8")
        if len(items) and 최다[1] / len(items) > 0.30:
            위반.append(f"최다비목 {최다[1]/len(items):.0%}>30%")
        미사용 = [a for a in 축 if a not in 이유]
        if 미사용:
            위반.append("이유축 미사용:" + "·".join(미사용))
        비운값 = set(이유널) - {None}
        if 비운값:
            위반.append(f"가능/판단불가에 판정이유 값 있음(규약③): {sorted(비운값)}")
        복수 = sum(1 for i in 분모
                   if isinstance((i.get("입력필드") or {}).get("판정이유"), list)
                   and len((i.get("입력필드") or {}).get("판정이유")) > 1)
        if 복수:
            위반.append(f"판정이유가 배열(축 1개 규격 위반) {복수}건")

        print(json.dumps({
            "사업": 사업, "shape": shape, "문항": len(items),
            "판정": dict(판정), "세트": dict(세트), "대상": dict(대상), "평가범위": dict(범위),
            "비목종": len(비목), "최다비목": 최다[0], "최다수": 최다[1],
            "이유분모": len(분모), "이유": dict(이유), "가능판단불가_이유값": dict(이유널),
            "대조방식": dict(방식), "규격위반": 위반,
        }, ensure_ascii=False))
        for k, v in list(판정.items()):
            총["판정:" + k] += v
        for k, v in list(세트.items()):
            총["세트:" + k] += v
        for k, v in list(대상.items()):
            총["대상:" + k] += v
        for k, v in list(이유.items()):
            총["이유:" + str(k)] += v
        for k, v in list(방식.items()):
            총["방식:" + k] += v
        총["문항"] += len(items)
        총["이유분모"] += len(분모)
    print(json.dumps({"합계": dict(총)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
