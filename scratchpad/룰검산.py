# -*- coding: utf-8 -*-
"""룰 제안 JSON 검산기 — 사람이 읽는 것과 «닻이 다른» 자동 검사.

사람은 의미를 보고 이 검사기는 «문자열» 을 본다. 서로의 사각을 메운다.
DB 는 읽기만 한다. 아무것도 안 고친다.

    PYTHONIOENCODING=utf-8 python scratchpad/룰검산.py scratchpad/룰제안_R1.json
    PYTHONIOENCODING=utf-8 python scratchpad/룰검산.py scratchpad/룰제안_R1.json --계수

🔴 «실패» 와 «확인 불가» 를 갈라 센다. 못 잰 것을 통과로 세면 검사기가 무력해진다.
   (2026-09-05 에 하루 종일 나온 실수다 — 「없다」와 「못 봤다」를 안 갈랐다)
"""
from __future__ import annotations
import json, re, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
from _lib import db  # noqa: E402

단위상한 = 12
불가어휘 = ("집행불가", "집행 불가", "집행할 수 없", "사용 불가", "지급 불가",
            "인정하지 않", "불인정", "허용하지 않")
허용값 = {"가능", "조건부", "불가"}
한도유형값 = {"금액", "비율", "개수"}


def 납작(s: str) -> str:
    """표가 눌려 세목명이 줄로 쪼개진 원문 대응 — 공백을 전부 지운다."""
    return re.sub(r"\s+", "", s or "")


def 검사(제안: list[dict], cur) -> tuple[list, dict]:
    결과 = []
    집계 = {"통과": 0, "거부": 0, "확인불가": 0}
    for i, e in enumerate(제안, 1):
        문제, 유보 = [], []
        rid = e.get("rule_id")
        칸 = e.get("칸") or {}

        # ── 1. rule_id 실재 + 사업명·비목 일치 ──────────────────────
        cur.execute("select 사업명, 비목, 허용 from corpus.rules where rule_id=%s", (rid,))
        행 = cur.fetchone()
        if 행 is None:
            문제.append(f"rule_id {rid} 가 corpus.rules 에 없다")
            행 = (None, None, None)
        else:
            # 🔴 «안 적은 것» 을 통과로 세지 않는다 — 이 검사기의 주제 그대로다.
            for 칸이름, 디비값 in (("사업명", 행[0]), ("비목", 행[1])):
                if 칸이름 not in e:
                    유보.append(f"{칸이름} 을 안 적었다 — DB 값 '{디비값}' 과 대조 못 했다")
                elif e[칸이름] != 디비값:
                    문제.append(f"{칸이름} 불일치 — 제안 '{e[칸이름]}' vs DB '{디비값}'")

        # ── 2. 값 형식 ──────────────────────────────────────────────
        if "허용" in 칸 and 칸["허용"] not in 허용값:
            문제.append(f"허용 값이 이상하다: {칸['허용']!r} (가능/조건부/불가 만)")
        if "한도_유형" in 칸 and 칸["한도_유형"] not in 한도유형값:
            문제.append(f"한도_유형이 이상하다: {칸['한도_유형']!r} (금액/비율/개수 만)")
        단위 = 칸.get("한도_단위")
        if 단위 and len(단위) > 단위상한:
            문제.append(f"한도_단위가 {len(단위)}자 — 상한 {단위상한}자. "
                        f"문장을 밀어넣지 말고 사전승인_조건 으로 보내라: {단위[:40]!r}")
        if ("한도_값" in 칸) != ("한도_유형" in 칸):
            문제.append("한도_값 과 한도_유형 은 «같이» 넣는다")
        if 칸.get("한도_값") is not None and not isinstance(칸["한도_값"], (int, float)):
            문제.append(f"한도_값은 숫자여야 한다: {칸['한도_값']!r}")

        # ── 3. 근거 조가 실재하나 + 원문발췌가 «문자 그대로» 있나 ────
        발췌 = (e.get("원문발췌") or "").strip()
        근거 = e.get("근거") or []
        if not 근거:
            문제.append("근거가 비었다 — [{doc_id, 조번호}] 필수")
        if not 발췌:
            문제.append("원문발췌가 비었다 — 검산의 유일한 근거다")
        찾음 = None
        for g in 근거:
            cur.execute("select 본문 from corpus.doc_articles where doc_id=%s and 조번호=%s",
                        (g.get("doc_id"), g.get("조번호")))
            r = cur.fetchone()
            if r is None:
                문제.append(f"근거 조가 없다: {g.get('doc_id')} {g.get('조번호')}")
                continue
            본문 = r[0] or ""
            if 발췌 and 발췌 in 본문:
                찾음 = "정확일치"; break
            if 발췌 and 납작(발췌) in 납작(본문):
                찾음 = "공백무시일치"; break
        if 발췌 and 근거 and 찾음 is None:
            문제.append("원문발췌가 근거 조 본문에 «없다» — 요약·윤문했거나 다른 조다")

        # ── 4. 불가로 바꾼 행은 원문 근거를 요구한다 ────────────────
        if 칸.get("허용") == "불가":
            본문들 = []
            for g in 근거:
                cur.execute("select 본문 from corpus.doc_articles where doc_id=%s and 조번호=%s",
                            (g.get("doc_id"), g.get("조번호")))
                r = cur.fetchone()
                if r: 본문들.append(납작(r[0] or ""))
            if not 본문들:
                유보.append("허용=불가 인데 근거 본문을 못 읽었다 — 확인 불가")
            elif not any(납작(w) in b for b in 본문들 for w in 불가어휘):
                문제.append("허용=불가 로 바꿨는데 근거 본문에 «집행불가»류 문구가 없다")

        # ── 5. 한도 초과 기재 표시 ──────────────────────────────────
        조건문 = 칸.get("사전승인_조건") or ""
        숫자 = re.findall(r"\d[\d,]*\s*(?:%|원|만원|대|건|일|개월)", 조건문)
        if 숫자 and not e.get("한도초과기재"):
            # 한도_값 이 «비어 있는데» 조건문에만 숫자가 있으면 그 한도는 코드가 못 읽는다.
            # 한도_값 이 이미 있으면 조건문이 그 값을 풀어 쓴 것이라 정상이다.
            if 칸.get("한도_값") is None:
                유보.append(f"한도_값이 비었는데 조건문에 숫자 한도가 있다 {숫자[:2]} — "
                            f"한도_값 으로 옮기거나 한도초과기재 true 로 표시해라")

        상태 = "거부" if 문제 else ("확인불가" if 유보 else "통과")
        집계[상태] += 1
        결과.append({"n": i, "rule_id": rid, "상태": 상태, "일치": 찾음,
                     "문제": 문제, "유보": 유보,
                     "사업명": 행[0], "비목": 행[1], "기존허용": 행[2],
                     "새허용": 칸.get("허용")})
    return 결과, 집계


def 사전계수(제안: list[dict], cur) -> None:
    """전량 run 을 돌리기 «전» 에 「효과를 잴 수 있나」를 판단하는 근거.

    🔴 2026-09-05 ai-04 수정 — 두 곳이 틀려 있었다. 둘 다 «태워 보고» 잡았다.
       ⑴ 사업명이 NULL 인 룰(9/74행 = 전 사업 공통)이 계수에서 통째로 빠졌다.
          그 9행의 비목은 골든 253/295 문항에 걸리는데 계수기가 「0건」을 찍었다.
          **방향이 최악이다 — 가장 넓은 룰이 「효과 없음」으로 읽힌다.**
       ⑵ 게이트가 «허용 변경 행수» 를 봤다. 정지 조건은 McNemar 의 «불일치 쌍 m»
          에 걸리는 것이지 룰 행수에 걸리는 게 아니다. 행수와 m 사이엔 두 단계가 있다:
              룰 행수  →  걸리는 골든 문항 n  →  실제 뒤집힌 문항 m   (m ≤ n)
          그래서 «n ≤ 5 면 못 잰다» 만 증명 가능하다. 그 위는 «가능성» 일 뿐이다.
       ⑶ 사업명·비목은 «제안이 적은 값» 이 아니라 **DB 에서 읽는다.** 제안이 필드를
          빼면 예전 코드는 조용히 통과시켰다.
    """
    바뀜 = [e for e in 제안 if (e.get("칸") or {}).get("허용") in ("불가", "가능")]
    print()
    print("=" * 68)
    print("사전 계수 — GPU 를 켜기 «전» 에 알 수 있는 것")
    print("=" * 68)
    불가 = sum(1 for e in 바뀜 if e["칸"]["허용"] == "불가")
    가능 = sum(1 for e in 바뀜 if e["칸"]["허용"] == "가능")
    print(f"  허용을 바꾼 행   {len(바뀜)}행  (->불가 {불가} · ->가능 {가능})")
    if not 바뀜:
        print("  🔴 0행 — 이 제안은 판정을 바꾸지 않는다. 효과 측정 자체가 성립 안 한다")
        return

    ids = [e.get("rule_id") for e in 바뀜]
    cur.execute("select rule_id, 사업명, 비목 from corpus.rules where rule_id = any(%s)", (ids,))
    메타 = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    유령 = [i for i in ids if i not in 메타]
    공통비목 = sorted({b for s, b in 메타.values() if b and not s})
    쌍 = sorted({(s, b) for s, b in 메타.values() if s and b})
    if 유령:
        print(f"  🔴 DB 에 없는 rule_id {len(유령)}건 {유령[:5]} — 이 행들은 계수에서 빠졌다")
    if 공통비목:
        print(f"  ℹ️ 사업명이 NULL(전 사업 공통)인 행이 있다 — 비목만으로 센다: {공통비목}")

    cur.execute("""select count(*) from eval.golden_set g
                   where (g.평가범위 is null or g.평가범위 not like '범위밖%%')
                     and ( g.비목 = any(%s::text[])
                           or (g.사업명, g.비목)
                               in (select * from unnest(%s::text[], %s::text[])) )""",
                (공통비목, [s for s, _ in 쌍], [b for _, b in 쌍]))
    n = cur.fetchone()[0]
    cur.execute("""select count(*) from eval.golden_set
                   where (평가범위 is null or 평가범위 not like '범위밖%%') and 비목 is null""")
    미상 = cur.fetchone()[0]
    print(f"  그 행이 걸리는 골든 문항 n = {n}건")
    print(f"  ⚠️ 비목이 없어 «못 센» 문항 {미상}건 — n 은 그만큼 과소일 수 있다")
    print()
    print("  읽는 규칙:  룰 행수 → 걸리는 문항 n → 실제 뒤집힌 문항 m.   m ≤ n 이다.")
    if n <= 5:
        print(f"  🔴 n={n} ≤ 5 — **못 잰다가 증명된다.** m ≤ n ≤ 5 이고, McNemar 는")
        print("     불일치 5건 이하면 어떤 쏠림이 나와도 유의하지 않다(5/0 도 p=0.0625).")
        print("     → 룰은 넣되 «효과 주장 안 함» 으로 가고, run 전에 중앙에 알려라.")
        print("     🔴 이건 «효과가 없다» 가 아니다. «이 표본으로는 못 잰다» 다. 갈라 써라.")
    else:
        print(f"  ⚠️ n={n} > 5 — 못 잰다고 «증명되지는» 않는다. 그뿐이다.")
        print("     n 은 상한이라 실제 m 은 이보다 훨씬 작을 수 있다. m 은 run 뒤에만 안다.")
        print("     문턱표는 scratchpad/부분셋100_설계.md 의 정확검정 표를 봐라.")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__); return 2
    경로 = sys.argv[1]
    제안 = json.load(open(경로, encoding="utf-8"))
    if not isinstance(제안, list):
        print("🔴 최상위가 배열이 아니다"); return 2

    c = db.connect(connect_timeout=5)
    cur = c.cursor()
    결과, 집계 = 검사(제안, cur)

    print("=" * 68)
    print(f"룰 제안 검산 — {경로}  ({len(제안)}건)")
    print("=" * 68)
    for r in 결과:
        표 = {"통과": "OK  ", "거부": "🔴거부", "확인불가": "⚠️유보"}[r["상태"]]
        일치 = f" [{r['일치']}]" if r["일치"] else ""
        바뀜 = f" {r['기존허용']}->{r['새허용']}" if r["새허용"] and r["새허용"] != r["기존허용"] else ""
        print(f"{표} #{r['n']:3d} rule_id={r['rule_id']} "
              f"{str(r['사업명'])[:14]:16s}{str(r['비목']):12s}{바뀜}{일치}")
        for m in r["문제"]: print(f"        🔴 {m}")
        for m in r["유보"]: print(f"        ⚠️  {m}")

    print("-" * 68)
    print(f"통과 {집계['통과']} · 🔴거부 {집계['거부']} · ⚠️확인불가 {집계['확인불가']}")
    print("🔴 «확인불가» 를 통과로 세지 마라 — 못 잰 것이지 맞은 것이 아니다.")

    if "--계수" in sys.argv:
        사전계수(제안, cur)

    return 1 if 집계["거부"] else 0


if __name__ == "__main__":
    sys.exit(main())
