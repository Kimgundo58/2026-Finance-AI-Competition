# -*- coding: utf-8 -*-
"""산문 컬럼 섞임(다단 좌표 컬럼 분리 실패) 신호 스캔 — ai-8c 배정, ai-be 수행 (2026-09-06)

배경: 어A 작업 중 창업중심대학 참고3 「기계장치」조 산문에서 다단 컬럼이 섞인 걸 발견.
     0단계 표 복구(표행 0->258)는 «표»만 고쳤고, 표 밖 «산문»이 성한지는 아무도 안 쟀다.
     이 스크립트가 그 신호를 정의하고 전수로 센다. «고치지 않는다» — 세기만 한다.

신호 정의 (재현 가능하게 고정)
  표줄(마크다운 '|' 행, '---' 구분줄)은 제외하고 남은 '산문' 줄만 본다.

  신호 A (조각줄 산개형) — 실제 사례에서 놓쳤던 형태라 추가함
    공백 제거 길이 <= FRAG_LEN(4) 인 '조각줄'이 한 조에 FRAG_MIN(2)번 이상.
    페이지번호줄("- 23 -")은 그 자체로는 안 센다(그건 컬럼섞임이 아니라 그냥 쪽번호다)

  신호 B (연속 짧은줄형)
    공백 제거 길이 <= SHORT_LEN(12) 인 줄이 RUN_MIN(3)줄 이상 연속

  A 또는 B 중 하나라도 걸리면 '의심 조'.

🔴 이 신호에 ✅ 는 없다 — «짧은 줄이 몰려 있다»만 잰다. 진짜 깨졌는지는 사람이 원문을 봐야 한다.
   scratchpad/산문컬럼_깨짐조사.md 의 표본 검증 결과를 같이 봐라.

사용: PYTHONIOENCODING=utf-8 python scratchpad/컬럼깨짐_스캔.py
      -> scratchpad/산문컬럼_깨짐_전체.json 에 조별 결과 저장 (DB 읽기 전용, 아무것도 안 고침)
"""
import sys, os, json, re

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "_lib"))
import db  # noqa: E402

FRAG_LEN = 4
FRAG_MIN = 2
SHORT_LEN = 12
RUN_MIN = 3

페이지번호_패턴 = re.compile(r"^[-–]\s*\d+\s*[-–]$")


def 표줄인가(line):
    s = line.strip()
    if not s:
        return False
    if s.startswith("|"):
        return True
    if re.fullmatch(r"[-|: ]+", s):
        return True
    return False


def 산문줄들(본문):
    lines = 본문.split("\n")
    return [(i, l) for i, l in enumerate(lines) if not 표줄인가(l) and l.strip()]


def 신호A_조각줄(산문):
    조각 = []
    for i, l in 산문:
        s = re.sub(r"\s+", "", l)
        if not s or 페이지번호_패턴.match(l.strip()):
            continue
        if len(s) <= FRAG_LEN:
            조각.append((i, l))
    return 조각 if len(조각) >= FRAG_MIN else []


def 신호B_연속짧은줄(산문):
    구간, run = [], []
    for i, l in 산문:
        길이 = len(re.sub(r"\s+", "", l))
        if 0 < 길이 <= SHORT_LEN:
            run.append((i, l))
        else:
            if len(run) >= RUN_MIN:
                구간.append(list(run))
            run = []
    if len(run) >= RUN_MIN:
        구간.append(list(run))
    return 구간


def 검사(본문):
    산문 = 산문줄들(본문)
    return 신호A_조각줄(산문), 신호B_연속짧은줄(산문)


def main():
    with db.connect() as c, c.cursor() as cur:
        cur.execute(
            "select d.doc_id, d.layer, a.조번호, a.본문 "
            "from corpus.doc_articles a join corpus.documents d on d.doc_id=a.doc_id "
            "where d.status='active' and not a.삭제 and d.layer in ('L1','L2')"
        )
        rows = cur.fetchall()

    print(f"대상 조 수 = {len(rows)}")

    결과 = []
    for doc_id, layer, 조번호, 본문 in rows:
        if not 본문:
            continue
        A, B = 검사(본문)
        if A or B:
            결과.append({
                "doc_id": doc_id, "layer": layer, "조번호": 조번호,
                "A_조각줄수": len(A), "B_구간수": len(B),
                "A_표본": [l for (i, l) in A[:5]],
                "B_표본": [l for (i, l) in (B[0] if B else [])[:3]],
            })

    결과.sort(key=lambda x: -(x["A_조각줄수"] + x["B_구간수"]))
    print(f"신호 걸린 조 수 = {len(결과)} / {len(rows)}")

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "산문컬럼_깨짐_전체.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(결과, f, ensure_ascii=False, indent=2)
    print(f"저장: {out}")

    return 결과


if __name__ == "__main__":
    main()
