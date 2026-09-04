# -*- coding: utf-8 -*-
"""중앙(ai-c5) — 오늘 고치는 룰 «다섯 행 · 각 금지예시 한 원소» 만 적용한다.

🔴 기본은 dry-run. `--apply` 를 줘야 쓴다.
🔴 `corpus.rules` 는 판정기가 실시간으로 읽는 **라이브** 테이블이다. 그래서:
   ① 적용 전 74행 스냅샷이 파일로 떠 있어야 한다(`_스냅샷_적용전_74행.json`)
   ② 행 통째 교체가 아니라 **금지예시 배열의 한 원소만** 바꾼다
   ③ 바꾸기 «전» 과 «후» 에 `금지적중()` 을 실제로 태워 의도한 변화만 일어나는지 본다
   ④ COMMIT 이 성공을 돌려줘도 안 믿는다 — **새 연결**로 다시 읽어 확인한다
   ⑤ 되돌리는 SQL 을 출력한다

왜 이 다섯뿐인가 — 「닻 2개(값 «과» 원인 진단)가 같아야 고친다」를 통과한 게 이것뿐이다.
  · 440: 초기창업 퇴직급여충당금. 2025 개정 미반영(근거는 2025년판·값은 2024년판).
         🔴 예비 430 은 «정확하다» — 예비는 2025년판도 집행불가라 건드리지 않는다.
         (「예비를 복사했다」로 오진했으면 멀쩡한 430 까지 고쳤을 자리다)
  · 450: 초격차 차량임차. 근거엔 「사전 검토 후 집행 가능」 예외가 있는데 금지예시엔 없다.
         「(사전승인 시 예외)」 표기는 저장소에 이미 17건 있는 관용이다.
  · 449·458·467: 「4대사회보험 미가입」 계통. 세 사업의 근거가 모두 «제도상 가입 불가자는 예외»
         (만 60세 이상 국민연금 제외 · 만 65세 이후 고용 고용보험 제외 · 외국인)를 명시하는데
         금지예시는 무조건이다. 🔴 세 행 다 **허용예시에 그 예외가 이미 들어 있어 자기모순**이었다.

안 고치는 것 — 닻이 갈렸거나 스키마가 없다:
  · 439 성공보수(오탐 — 괄호가 «다른 품목» 을 가리킨다) · 456 창업중심대학 생활가전(부분배제라
    풀면 집기·가구까지 열린다) · 핵 단축 39건 · TIPS 신규 11행 · 477 지역별 한도 · item_vocab
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import _lib.db as db          # noqa: E402
from rule_lookup import 금지예시_해부, 금지적중   # noqa: E402

스냅샷 = Path(__file__).resolve().parent / "_스냅샷_적용전_74행.json"

# rule_id → (바꿀 원소의 현재 문자열, 바꿀 문자열)
수정 = {
    440: ("퇴직급여충당금",
          "1년 미만 근무 인력의 퇴직급여충당금"),
    450: ("차량(승용차·화물차·이륜자동차 등) 임차 경비",
          "차량(승용차·화물차·이륜자동차 등) 임차 경비(사전 검토·승인 시 예외)"),
    # 「4대사회보험 미가입」 계통 — 세 사업의 근거가 모두 «제도상 가입 불가자는 예외» 를 명시한다.
    # 세 행 다 허용예시에 「만 60세 이상…」 이 이미 들어 있어 **같은 행 안에서 자기모순**이었다.
    449: ("4대사회보험 미가입 근로자(단기근로자 포함)",
          "4대사회보험 미가입 근로자(단기근로자 포함. 제도상 가입 불가자는 예외)"),
    458: ("4대사회보험 미가입 직원",
          "4대사회보험 미가입 직원(제도상 가입 불가자는 증빙 제출 시 예외)"),
    467: ("4대사회보험 미가입 신규 채용인력",
          "4대사회보험 미가입 신규 채용인력(제도상 가입 불가자는 예외)"),
}

# (rule_id, 품목, 용도, 사업명, 비목, 기대변화) — 기대변화: '적중→통과' | '변화없음'
검사 = [
    (440, "퇴직급여충당금", "2년 근속 직원 퇴직급여충당금 적립",
     "초기창업패키지", "인건비", "적중→통과"),
    (440, "퇴직급여충당금", "1년 미만 근무 인력의 퇴직급여충당금 적립",
     "초기창업패키지", "인건비", "변화없음"),
    (440, "퇴직급여충당금", "2년 근속 직원 퇴직급여충당금 적립",
     "예비창업패키지", "인건비", "변화없음"),   # 🔴 예비 430 은 안 건드린다 — 계속 적중해야 한다
    (450, "차량 임차 경비", "사업 연관성 있어 주관기관 사전검토 받은 화물차 임차",
     "초격차 스타트업 프로젝트", "지급수수료", "적중→통과"),
    (450, "기자재 임차", "서버 임차",
     "초격차 스타트업 프로젝트", "지급수수료", "변화없음"),
    (449, "4대사회보험 미가입 근로자", "만 65세 이후 고용된 자로 고용보험 가입 제외 대상",
     "초격차 스타트업 프로젝트", "인건비", "적중→통과"),
    (458, "4대사회보험 미가입 직원", "만 65세 이후 고용된 자로 고용보험 가입 제외 대상",
     "창업중심대학", "인건비", "적중→통과"),
    (467, "4대사회보험 미가입 신규 채용인력", "만 60세 이상 국민연금 가입 제외 대상",
     "창업도약패키지", "인건비", "적중→통과"),
    # 🔴 «잃는 것» 을 눈에 보이게 남긴다 — 진짜 미가입자도 즉답을 잃고 정상 경로로 내려간다.
    #    440 과 같은 오답 비대칭 교환이다(즉답을 놓치는 것이지 판정을 놓치는 게 아니다).
    (449, "4대사회보험 미가입 근로자(단기근로자 포함)", "가입 의무가 있는데 미가입",
     "초격차 스타트업 프로젝트", "인건비", "적중→통과"),
]


def 적중표(cur) -> dict:
    out = {}
    for i, (rid, 품목, 용도, 사업, 비목, _) in enumerate(검사):
        h = 금지적중(cur, 품목, 용도, 사업, 비목)
        out[i] = (h or {}).get("rule_id"), (h or {}).get("예시_원문")
    return out


def main(적용: bool) -> int:
    if not 스냅샷.exists():
        print(f"🔴 스냅샷이 없다: {스냅샷}  — 먼저 뜨고 와라.")
        return 1
    snap = json.loads(스냅샷.read_text(encoding="utf-8"))
    print(f"── 스냅샷 {snap['뜬시각']} · {snap['행수']}행")

    with db.connect(autocommit=True) as conn:
        cur = conn.cursor()
        for rid, (old, new) in 수정.items():
            cur.execute("select 사업명, 비목, 금지예시 from corpus.rules where rule_id=%s", [rid])
            r = cur.fetchone()
            if not r:
                print(f"🔴 rule {rid} 이 없다."); return 1
            if old not in r[2]:
                print(f"🔴 rule {rid} 의 금지예시에 {old!r} 가 없다 — 이미 고쳐졌거나 값이 다르다.")
                print(f"   현재: {r[2]}")
                return 1
            h구 = 금지예시_해부(old); h신 = 금지예시_해부(new)
            print(f"\n[rule {rid}] {r[0]} / {r[1]}")
            print(f"  {old!r}  무조건={h구['무조건']}")
            print(f"→ {new!r}  무조건={h신['무조건']} 단서={h신['예외단서']}")
        before = 적중표(cur)

    if not 적용:
        print("\n── 적용 전 적중 상태")
        for i, (rid, 품목, 용도, _, 비목, 기대) in enumerate(검사):
            print(f"  [{rid}] {품목} | {용도[:28]} → rule {before[i][0]}  (기대: {기대})")
        print("\n(dry-run — 아무것도 안 썼다. 실제 적용은 --apply)")
        return 0

    with db.connect() as conn:                       # 트랜잭션 하나
        cur = conn.cursor()
        for rid, (old, new) in 수정.items():
            cur.execute("SAVEPOINT s1")
            try:
                cur.execute(
                    "update corpus.rules set 금지예시 = array_replace(금지예시, %s, %s) "
                    "where rule_id = %s", [old, new, rid])
                if cur.rowcount != 1:
                    raise RuntimeError(f"rowcount={cur.rowcount}")
                cur.execute("RELEASE SAVEPOINT s1")
                print(f"  update rule {rid} ok")
            except Exception as e:                   # noqa: BLE001
                cur.execute("ROLLBACK TO SAVEPOINT s1")
                print(f"🔴 rule {rid} 실패: {e} — 전체 중단")
                conn.rollback()
                return 1
        conn.commit()

    # 🔴 새 연결로 다시 읽는다. COMMIT 의 성공 반환을 믿지 않는다.
    실패 = 0
    with db.connect(autocommit=True) as conn2:
        cur2 = conn2.cursor()
        for rid, (old, new) in 수정.items():
            cur2.execute("select 금지예시 from corpus.rules where rule_id=%s", [rid])
            현재 = cur2.fetchone()[0]
            ok = (new in 현재) and (old not in 현재)
            print(f"  확인 rule {rid}: {'✅' if ok else '🔴 반영 안 됨'}")
            실패 += 0 if ok else 1
        after = 적중표(cur2)

    print("\n── 적중 변화")
    for i, (rid, 품목, 용도, _, 비목, 기대) in enumerate(검사):
        전, 후 = before[i][0], after[i][0]
        실제 = "적중→통과" if (전 and not 후) else ("변화없음" if 전 == 후 else f"{전}→{후}")
        표 = "✅" if 실제 == 기대 else "🔴"
        print(f"  {표} [{rid}] {품목} | {용도[:28]} : {실제} (기대 {기대})")
        실패 += 0 if 실제 == 기대 else 1

    print("\n── 되돌리려면:")
    for rid, (old, new) in 수정.items():
        print(f"  update corpus.rules set 금지예시 = array_replace(금지예시, "
              f"'{new}', '{old}') where rule_id = {rid};")
    print("🔴 캐시: v7(_설정_해시 = 내용해시)이 배포돼 있으면 자동 무효화된다. 아니면 ai-c4 에 알려라.")
    return 1 if 실패 else 0


if __name__ == "__main__":
    raise SystemExit(main("--apply" in sys.argv))
