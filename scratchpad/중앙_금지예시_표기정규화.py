# -*- coding: utf-8 -*-
"""금지예시의 «서술 꼬리» 를 떼어 명사구로 만든다 (오너 지시 2026-09-06).

🔴 --dry 가 기본. --apply 를 줘야 쓴다.
🔴 «내용» 은 안 바꾼다. 「…는 집행할 수 없다」 -> 「…」 로 «꼬리만» 뗀다.
   게이트 A 매칭이 「금지예시 핵 ⊂ 품목」이라, 꼬리가 붙어 있으면 사용자 문장에 «절대» 안 들어간다.
🔴 예외단서(괄호)는 «건드리지 않는다» — 그게 조건부/무조건을 가르는 축이다.
"""
import argparse, re, sys
sys.path.insert(0, 'scripts/_lib'); import db

_꼬리 = re.compile(
    r"(?:은|는|이|가)?\s*(?:사업비에서|사업비로)?\s*"
    r"(?:집행|구매|사용|지급)\s*(?:할\s*수\s*없다|불가|불가능|금지)\s*\.?\s*$")

def 정규화(s: str) -> str:
    새 = _꼬리.sub("", s).strip().rstrip(",·").strip()
    return 새 or s          # 통째로 사라지면 원본 유지

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--apply", action="store_true"); a = ap.parse_args()
    바꿈 = 0
    with db.connect() as c, c.cursor() as cur:
        cur.execute("select rule_id, 사업명, 비목, 금지예시 from corpus.rules "
                    "where 금지예시 is not null order by rule_id")
        for rid, 사업, 비목, 금지 in cur.fetchall():
            새목록, 변경 = [], False
            for x in 금지:
                y = 정규화(x)
                if y != x:
                    변경 = True
                    print(f"  rule {rid:4d} {str(사업)[:9]:11s}{str(비목)[:9]:11s}")
                    print(f"        - {x[:78]}")
                    print(f"        + {y[:78]}")
                    바꿈 += 1
                # 🔴 중복이 생기면 «접는다» — 꼬리를 떼면 같아지는 항목이 있을 수 있다
                if y not in 새목록:
                    새목록.append(y)
            if 변경 and a.apply:
                cur.execute("update corpus.rules set 금지예시=%s::text[] where rule_id=%s",
                            (새목록, rid))
                assert cur.rowcount == 1
        if a.apply: c.commit(); print("\nCOMMIT 반환됨")
    print(f"\n{'적용' if a.apply else 'DRY'} — 꼬리 제거 {바꿈}건")
    if not a.apply: print("🔴 아무것도 안 썼다. 쓰려면 --apply")

if __name__ == "__main__": main()
