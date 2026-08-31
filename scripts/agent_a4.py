# -*- coding: utf-8 -*-
"""A4 개정 대응 에이전트 — 판정 경로 밖의 비동기 배치 (`Agent.md` §9 우선순위 1).

무엇을 푸는가
  규범이 개정되면 `corpus.rules` 의 `근거` 가 가리키는 좌표(doc_id, 조번호)가 조용히
  낡는다. 조 번호는 개정 때 밀린다 — 통합관리지침 제12차 제33~42조가 제14차에서
  제36~45조다. **번호로는 못 잇는다.** 그래서 조 매칭은 **제목이 주 · 번호가 보조**다.

절차 (Agent.md §9 A4)
  ① 계보 짝짓기      구판 → 신판 (같은 규범의 이전/현재 판)
  ② 조 매칭          제목 정확 → 번호 정확 → 제목 유사(0.85) → 신설/삭제
  ③ 본문 diff        difflib 비율 + 조번호 이동 여부
  ④ 근거 역조회      변경된 조를 `근거` JSONB 로 삼는 rules·check_items·precedence 조회
  ⑤ recheck_queue    적재

🔴 **`corpus.rules` 를 UPDATE 하지 않는다.** Agent.md 는 "해당 룰 verified=false 전환"
   이라고 쓰여 있으나 2026-08-31 밤 8세션 병렬에서 G 세션이 `rules` 를 TRUNCATE 재적재
   하므로 계약서(`0831_최종구현.md` §3)가 H1 의 출력을 `corpus.recheck_queue` 로 못박았다.
   verified 전환은 큐를 사람이 처리할 때 일어난다. 이 파일은 큐까지만 쓴다.

왜 임베딩을 안 쓰는가
  `scripts/version_diff.py` 는 벡터로 조를 이었다. 그건 **탐색**용이고 이건 **배치**다.
  배치는 재현성이 요건이라(`Agent.md` §6) 같은 입력에 같은 큐가 나와야 한다.
  조제목은 개정에서 거의 안 바뀐다(실측 §아래) — 제목 매칭이 붙는 한 벡터는 불필요하고,
  KURE-v1 로딩 30초·GPU 경합도 없다. 제목이 안 붙는 잔여만 difflib 유사도로 잇는다.

두 개의 재료
  --source db   (기본) `corpus.documents` 의 active ↔ superseded 짝. 조문이 이미 DB 에
                있어 파싱이 없다. rules 근거가 가리키는 7개 문서가 전부 여기 있다
  --source xml  `법령 PDF/L1_법령/연혁/*.xml` 944건 ↔ 현행 XML. L1 법령 개정을 잡는다.
                rules 는 법령을 직접 인용하지 않으므로 `corpus.refs` 로 **깊이 1** 만
                타고 들어가 영향 룰을 찾는다 (깊이 2 이상은 CLAUDE.md 가 금지)

실행:
    PYTHONIOENCODING=utf-8 python scripts/agent_a4.py --dry            # 적재 없이 보고만
    PYTHONIOENCODING=utf-8 python scripts/agent_a4.py                  # DB 계보 → 큐
    PYTHONIOENCODING=utf-8 python scripts/agent_a4.py --source xml     # 법령 연혁 → 큐
    PYTHONIOENCODING=utf-8 python scripts/agent_a4.py --source both --heal

## 주 1회 배치 런북 — 이 순서를 지킨다

    python scripts/agent_a4.py --source both --heal    # 개정 감지 → 적재 → 치유 → 조정
    python scripts/agent_a2.py                         # L3 엄격조항 (L3 가 있을 때만)
    python scripts/agent_a5.py --in <기관답변.json>     # 답변이 왔을 때만

🔴 **`--heal` 은 적재 뒤에 온다.** 치유는 "죽은 대상ID 행에 살아있는 쌍둥이가 있으면
   지운다" 라서, 재실행이 현재 rule_id 로 쌍둥이를 먼저 만들어야 한다. 순서가 뒤집히면
   치유가 아무것도 못 지우고 큐에 죽은 행이 남는다.

⚠️ **스케줄러는 걸지 않았다.** 어디서 돌지가 안 정해졌다 (`서비스 아키텍쳐.md` §11 미결 #5
   호스팅). 배포처가 정해지면 위 세 줄을 그대로 cron/Task Scheduler 에 건다.
   지금은 수동 실행이고, 사람이 잊어도 큐가 낡을 뿐 판정은 안 틀린다 —
   큐는 판정 경로 밖이라서.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from difflib import SequenceMatcher, unified_diff
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")

# recheck_queue DDL 은 D 소유(D1-d)다. 아직 없으면 여기로 떨어뜨리고 계속 간다.
FALLBACK = ROOT / "scripts" / "_work" / "_recheck_queue.json"
REPORT = ROOT / "scripts" / "_work" / "_A4_개정보고.md"

동일_임계 = 0.995     # 이 이상이면 본문 무변경으로 본다 (공백·개정표기 흔들림 흡수)
제목유사_임계 = 0.85   # 제목이 정확히 안 붙을 때 이어붙이는 하한

비목_ENUM = ("재료비", "외주용역비", "기계장치", "인건비", "지급수수료",
             "여비", "교육훈련비", "광고선전비", "특허권등무형자산취득비", "창업활동비")


# ────────────────────────────────────────────────────────────────────
# 1. 계보 — 같은 규범의 판(版) 묶기
# ────────────────────────────────────────────────────────────────────

_차수 = re.compile(r"제?\s*(\d+)\s*차")
_연도 = re.compile(r"(19|20)(\d{2})\s*년")
_날짜 = re.compile(r"\b(19|20)\d{6}\b")

# 자동 family 키가 못 잇는 **제명 변경**만 손으로 잇는다. 근거를 함께 적는다.
# 추측이 아니라 문서 안에서 확인한 것만 넣는다 — 틀린 짝짓기는 없는 것보다 나쁘다.
_FAMILY_ALIAS = {
    # 제13차「창업사업화 지원사업 통합관리지침」→ 제14차「중소기업창업 지원사업
    # 통합관리지침」. 차수가 13→14 로 연속이고 발행주체(중기부)·조문 구성이 이어진다.
    "L1_창업사업화_지원사업_통합관리지침_제13차개정_20250205":
        "중소기업창업지원사업통합관리지침",
    # 「혁신분야 창업패키지(신산업 스타트업 육성)」가 「초격차 스타트업 1000 프로젝트」로
    # 개명. 2023년판 → 2024년판.
    "혁신분야 창업패키지(신산업 스타트업 육성) 세부관리기준(2023년)":
        "초격차스타트업프로젝트세부관리기준",
    # 2022년판은 제명에 '지원사업' 이 붙어 있다.
    "창업도약패키지 지원사업 세부관리기준(2022년)":
        "창업도약패키지세부관리기준",
}

# 계보로 볼 문서군. 모집공고·주관기관 현황·별지서식은 규범이 아니라 제외한다.
_규범명 = re.compile(r"(관리기준|관리지침|운영요령|운영지침|보조사업\s*관리규정)")


def family(doc_id: str) -> str | None:
    """판(版) 표기를 걷어낸 규범 이름. 같은 규범의 여러 판이 같은 키를 갖는다."""
    if doc_id in _FAMILY_ALIAS:
        return _FAMILY_ALIAS[doc_id]
    if not _규범명.search(doc_id):
        return None
    s = doc_id
    s = re.sub(r"^L[1-4]_", "", s)
    s = re.sub(r"\([^)]*\)", "", s)          # (2025년) (전면개정) (1) (신산업…)
    s = _날짜.sub("", s)
    s = _차수.sub("", s)
    s = _연도.sub("", s)
    s = re.sub(r"(전면)?개정본?", "", s)
    s = re.sub(r"[\s_·\.\-]", "", s)
    s = re.sub(r"\d+", "", s)                # '초격차 스타트업 1000' 의 1000
    s = re.sub(r"^(붙임|첨부)\d*", "", s)
    return s or None


def 판_순서(doc_id: str, 시행일) -> tuple:
    """판을 시간순으로 세우는 키 `(척도, 값, 보조)`.

    🔴 **척도가 다르면 비교하지 않는다.** 「초격차 …(제10차)」(차수 표기)와
    「초격차 …(2025년)」(연도 표기)는 같은 자로 잰 값이 아니다. 2025 > 10 이라고
    2025년판이 더 최신이라 단정하면 거짓 경고가 난다 (2026-08-31 실측: 그렇게 짰다가
    초격차·중기부보조사업관리규정 2건이 오탐이었다).
    """
    if 시행일:
        return (2, 시행일.year * 10000 + 시행일.month * 100 + 시행일.day, 0)
    m = _날짜.search(doc_id)
    if m:
        return (2, int(m.group(0)), 0)
    y = _연도.search(doc_id)
    c = _차수.search(doc_id)
    if y:
        return (1, int(y.group(0)[:4]), int(c.group(1)) if c else 0)
    if c:
        return (0, 0, int(c.group(1)))
    return (-1, 0, 0)


def 계보(conn) -> list[dict]:
    """{키, 판들:[(doc_id, status, 순서, 조문수)], 구, 신, 경고} 목록."""
    rows = conn.execute("""
        SELECT d.doc_id, d.status, d.시행일, count(a.article_id) AS n
        FROM corpus.documents d
        LEFT JOIN corpus.doc_articles a USING (doc_id)
        GROUP BY d.doc_id, d.status, d.시행일
    """).fetchall()

    묶음: dict[str, list] = {}
    for doc_id, status, 시행일, n in rows:
        k = family(doc_id)
        if not k:
            continue
        묶음.setdefault(k, []).append(
            {"doc_id": doc_id, "status": status, "순서": 판_순서(doc_id, 시행일), "조문수": n})

    out = []
    for k, 판들 in 묶음.items():
        판들.sort(key=lambda r: r["순서"])
        actives = [p for p in 판들 if p["status"] == "active"]
        구판들 = [p for p in 판들 if p["status"] != "active"]
        if not actives or not 구판들:
            continue
        # 현행은 status 가 정한다 — 판 표기가 아니라. active 가 곧 "지금 판정에 쓰는 판".
        신 = actives[-1]

        경고 = []
        for p in 구판들:
            # 척도가 같을 때만 비교한다. 다르면 판단하지 않는다 (거짓 경고 방지)
            if p["순서"][0] == 신["순서"][0] and p["순서"] > 신["순서"]:
                경고.append(("ACTIVE_NOT_LATEST", p["doc_id"]))

        # 구판 = 조문이 실제로 있는 것 중 가장 나중 판. 스캔 실패로 0조인 판은 못 쓴다
        후보 = [p for p in 구판들 if p["조문수"] >= 5]
        구 = 후보[-1] if 후보 else None
        # 같은 시행일이면 판이 아니라 **같은 규범의 다른 포맷**(XML vs 변환 PDF)이다.
        # 그대로 diff 하면 포맷 차이가 전부 '개정' 으로 나온다 — 잇지 않는다.
        if 구 and 구["순서"] == 신["순서"]:
            구 = None
        out.append({"키": k, "판들": 판들, "구": 구, "신": 신, "경고": 경고})
    out.sort(key=lambda g: g["키"])
    return out


# ────────────────────────────────────────────────────────────────────
# 2. 조 매칭 — 제목이 주, 번호가 보조
# ────────────────────────────────────────────────────────────────────

def _norm(s: str | None) -> str:
    """비교용 정규화. 공백·괄호 안 개정표기·문장부호를 걷어낸다."""
    s = s or ""
    s = re.sub(r"<개정[^>]*>", "", s)
    s = re.sub(r"\[전문개정[^\]]*\]", "", s)
    s = re.sub(r"[\s ]+", "", s)
    return s


def _제목키(조제목: str | None, 본문: str) -> str:
    """조제목이 비어 있으면 본문 머리의 '제N조(제목)' 에서 캐낸다."""
    t = _norm(조제목)
    if t:
        return t
    m = re.match(r"\s*제\s*\d+\s*조(?:의\s*\d+)?\s*\(([^)]{1,40})\)", 본문 or "")
    return _norm(m.group(1)) if m else ""


def 조_읽기(conn, doc_id: str) -> list[dict]:
    rows = conn.execute("""
        SELECT 조번호, 조제목, 본문 FROM corpus.doc_articles
        WHERE doc_id = %s AND NOT 삭제 ORDER BY article_id
    """, (doc_id,)).fetchall()
    return [{"조번호": a, "조제목": b, "본문": c or "",
             "제목키": _제목키(b, c or ""), "본문키": _norm(c)} for a, b, c in rows]


def 조_매칭(구: list[dict], 신: list[dict]) -> list[dict]:
    """1:1 배정. (구, 신, 근거) — 근거는 제목|번호|제목유사|신설|삭제."""
    쌍, 쓴구, 쓴신 = [], set(), set()

    def 유일(목록, key, 값):
        h = [i for i, a in enumerate(목록) if a[key] == 값]
        return h[0] if len(h) == 1 else None

    # ① 제목 정확 일치 — 양쪽에서 유일할 때만. 제목은 개정에서 거의 안 바뀐다
    for j, n in enumerate(신):
        if not n["제목키"]:
            continue
        i = 유일(구, "제목키", n["제목키"])
        if i is None or i in 쓴구:
            continue
        if 유일(신, "제목키", n["제목키"]) != j:
            continue
        쌍.append({"구": 구[i], "신": n, "근거": "제목"})
        쓴구.add(i); 쓴신.add(j)

    # ② 남은 것은 조번호 정확 일치 — 제목이 비었거나 중복인 조
    구번호 = {a["조번호"]: i for i, a in enumerate(구)}
    for j, n in enumerate(신):
        if j in 쓴신:
            continue
        i = 구번호.get(n["조번호"])
        if i is None or i in 쓴구:
            continue
        쌍.append({"구": 구[i], "신": n, "근거": "번호"})
        쓴구.add(i); 쓴신.add(j)

    # ③ 남은 것은 제목 유사도. 임계 미만이면 잇지 않는다 — 틀린 짝은 없는 것보다 나쁘다
    for j, n in enumerate(신):
        if j in 쓴신 or not n["제목키"]:
            continue
        best, bi = 0.0, None
        for i, o in enumerate(구):
            if i in 쓴구 or not o["제목키"]:
                continue
            r = SequenceMatcher(None, o["제목키"], n["제목키"]).ratio()
            if r > best:
                best, bi = r, i
        if bi is not None and best >= 제목유사_임계:
            쌍.append({"구": 구[bi], "신": n, "근거": f"제목유사{best:.2f}"})
            쓴구.add(bi); 쓴신.add(j)

    for j, n in enumerate(신):
        if j not in 쓴신:
            쌍.append({"구": None, "신": n, "근거": "신설"})
    for i, o in enumerate(구):
        if i not in 쓴구:
            쌍.append({"구": o, "신": None, "근거": "삭제"})
    return 쌍


def 판정_변경(쌍: dict) -> dict:
    """변경유형·유사도·diff 요약."""
    구, 신 = 쌍["구"], 쌍["신"]
    if 구 is None:
        return {"변경유형": "신설", "유사도": 0.0, "본문변경": True,
                "요약": f"신설 {신['조번호']} {신['조제목'] or ''}".strip()}
    if 신 is None:
        return {"변경유형": "삭제", "유사도": 0.0, "본문변경": True,
                "요약": f"삭제 {구['조번호']} {구['조제목'] or ''}".strip()}

    sim = SequenceMatcher(None, 구["본문키"], 신["본문키"]).ratio()
    본문변경 = sim < 동일_임계
    번호이동 = 구["조번호"] != 신["조번호"]
    유형 = "번호이동" if 번호이동 else ("개정" if 본문변경 else "동일")
    return {"변경유형": 유형, "유사도": round(sim, 4), "본문변경": 본문변경,
            "요약": diff요약(구, 신, 번호이동, 본문변경, sim)}


def diff요약(구, 신, 번호이동: bool, 본문변경: bool, sim: float, 줄수: int = 6) -> str:
    """🔴 코드가 만든다. LLM 요약은 --llm 일 때만 — 배치의 재현성이 요건이다."""
    머리 = []
    if 번호이동:
        머리.append(f"조번호 {구['조번호']} → {신['조번호']}")
    if (구["조제목"] or "") != (신["조제목"] or ""):
        머리.append(f"조제목 「{구['조제목'] or ''}」 → 「{신['조제목'] or ''}」")
    if not 본문변경:
        return " · ".join(머리 + [f"본문 동일(유사도 {sim:.3f})"])

    변경 = [l for l in unified_diff((구["본문"] or "").splitlines(),
                                    (신["본문"] or "").splitlines(), n=0, lineterm="")
            if l[:1] in "+-" and l[:3] not in ("+++", "---")]
    보임 = [l[:1] + " " + l[1:].strip()[:90] for l in 변경[:줄수] if l[1:].strip()]
    꼬리 = f" (…외 {len(변경) - 줄수}줄)" if len(변경) > 줄수 else ""
    return " · ".join(머리 + [f"본문 개정(유사도 {sim:.3f})"]) + "\n" + "\n".join(보임) + 꼬리


# ────────────────────────────────────────────────────────────────────
# 3. 근거 역조회 — 변경된 조를 근거로 삼는 룰 찾기
# ────────────────────────────────────────────────────────────────────

def _근거조회(conn, doc_id: str, 조번호: str) -> list[dict]:
    """rules · check_items · precedence_rules 를 한 좌표로 역조회."""
    키 = json.dumps([{"doc_id": doc_id, "조번호": 조번호}], ensure_ascii=False)
    out = []
    for rid, 사업, 비목, 허용, ver in conn.execute("""
        SELECT rule_id, 사업명, 비목, 허용, verified FROM corpus.rules
        WHERE 근거 @> %s::jsonb ORDER BY rule_id
    """, (키,)).fetchall():
        out.append({"대상종류": "rule", "대상ID": str(rid), "사업명": 사업,
                    "비목": 비목, "상세": {"허용": 허용, "verified": ver}})
    for code, 사업, 비목 in conn.execute("""
        SELECT code, 사업명, 비목 FROM corpus.check_items
        WHERE 근거 @> %s::jsonb ORDER BY code
    """, (키,)).fetchall():
        out.append({"대상종류": "check_item", "대상ID": code, "사업명": 사업,
                    "비목": 비목, "상세": {}})
    # precedence_rules.근거 는 {doc: <src_path>, 조번호: '제3조(적용범위)'} 로
    # 모양이 다르다 (2026-08-31 실측). doc_id 가 아니라 경로라서 documents 로 되짚는다.
    for pid, 사업, 우선, 열위 in conn.execute("""
        SELECT p.prec_id, p.사업명, p.우선계층, p.열위계층
        FROM corpus.precedence_rules p, jsonb_array_elements(p.근거) e
        JOIN corpus.documents d ON d.src_path = e->>'doc'
        WHERE d.doc_id = %s AND e->>'조번호' LIKE %s
        ORDER BY p.prec_id
    """, (doc_id, 조번호 + "%")).fetchall():
        out.append({"대상종류": "precedence_rule", "대상ID": str(pid), "사업명": 사업,
                    "비목": None, "상세": {"우선계층": 우선, "열위계층": 열위}})
    return out


def 영향_레코드(conn, 신doc: str, 구doc: str, 쌍: dict, 변경: dict) -> list[dict]:
    """한 조의 변경 → recheck_queue 레코드들."""
    구a, 신a = 쌍["구"], 쌍["신"]
    recs: list[dict] = []

    def 붙이기(사유, 대상들, 조번호, 구조번호):
        for t in 대상들:
            recs.append({
                "종류": "A4개정", "사유코드": 사유,
                "대상종류": t["대상종류"], "대상ID": t["대상ID"],
                "사업명": t.get("사업명"), "비목": t.get("비목"),
                "doc_id": 신doc, "조번호": 조번호,
                "구doc_id": 구doc, "구조번호": 구조번호,
                "변경유형": 변경["변경유형"], "유사도": 변경["유사도"],
                "요약": 변경["요약"],
                "상세": {**t.get("상세", {}), "매칭근거": 쌍["근거"]},
            })

    if 변경["변경유형"] == "삭제":
        # 구판에만 있던 조. 그 좌표를 근거로 쓰는 룰이 있으면 근거가 사라진 것이다
        붙이기("BASIS_DELETED", _근거조회(conn, 구doc, 구a["조번호"]),
               None, 구a["조번호"])
        붙이기("BASIS_DELETED", _근거조회(conn, 신doc, 구a["조번호"]),
               구a["조번호"], 구a["조번호"])
        return recs

    if 변경["변경유형"] == "신설":
        return recs        # 신설은 영향 룰이 없다. 사람이 볼 요약에만 남는다

    if 변경["변경유형"] == "번호이동":
        # 🔴 가장 위험한 갈래. 근거가 **신판 문서 + 구판 번호** 로 적혀 있으면
        #    조회는 성공하는데 엉뚱한 조를 인용한다 (무음 오답).
        붙이기("BASIS_RENUMBERED", _근거조회(conn, 신doc, 구a["조번호"]),
               신a["조번호"], 구a["조번호"])
        붙이기("BASIS_RENUMBERED", _근거조회(conn, 구doc, 구a["조번호"]),
               신a["조번호"], 구a["조번호"])

    if 변경["본문변경"]:
        붙이기("BASIS_AMENDED", _근거조회(conn, 신doc, 신a["조번호"]),
               신a["조번호"], 구a["조번호"])
    return recs


def 어휘집_트리거(변경조: list[tuple[dict, dict]], 신doc: str, 구doc: str) -> list[dict]:
    """비목 어휘집 재검수 트리거 (`Agent.md` §9 A4 마지막 줄)."""
    맞은비목 = {}
    for 쌍, 변경 in 변경조:
        if not 변경["본문변경"] or not 쌍["신"]:
            continue
        for b in 비목_ENUM:
            if b in (쌍["신"]["본문"] or ""):
                맞은비목.setdefault(b, []).append(쌍["신"]["조번호"])
    return [{
        "종류": "A4개정", "사유코드": "ITEM_VOCAB_RECHECK",
        "대상종류": "item_alias", "대상ID": b, "사업명": None, "비목": b,
        # 조번호는 좌표 컬럼이다 — 여러 조를 쉼표로 이어 넣지 않는다. 목록은 상세에.
        "doc_id": 신doc, "조번호": None, "구doc_id": 구doc, "구조번호": None,
        "변경유형": "개정", "유사도": None,
        "요약": f"개정된 조 {len(조)}개가 「{b}」 를 언급한다 — 어휘집·별칭 재검수",
        "상세": {"조번호들": 조},
    } for b, 조 in sorted(맞은비목.items())]


# ────────────────────────────────────────────────────────────────────
# 4. XML 연혁 경로 — L1 법령 개정 → refs 깊이 1 → 영향 룰
# ────────────────────────────────────────────────────────────────────

XML_현행 = ROOT / "법령 PDF" / "L1_법령"
XML_연혁 = XML_현행 / "연혁"


def xml_계보() -> list[tuple[str, Path, Path]]:
    """(현행 doc_id, 직전 연혁 파일, 현행 파일). 파일명 규약:
       현행  L1_<법령명>_<시행일>.xml
       연혁  L1_<법령명>_<시행일>_<법령키>.xml
    """
    현행 = {}
    for p in XML_현행.glob("L1_*.xml"):
        m = re.match(r"^(.*)_(\d{8})$", p.stem)
        if m:
            현행.setdefault(m.group(1), []).append((int(m.group(2)), p))
    연혁: dict[str, list] = {}
    for p in XML_연혁.glob("L1_*.xml"):
        m = re.match(r"^(.*)_(\d{8})_\d+$", p.stem)
        if m:
            연혁.setdefault(m.group(1), []).append((int(m.group(2)), p))

    out = []
    for 이름, 판들 in 현행.items():
        판들.sort()
        날짜, 현행파일 = 판들[-1]
        앞 = sorted(x for x in 연혁.get(이름, []) if x[0] < 날짜)
        if 앞:
            out.append((현행파일.stem, 앞[-1][1], 현행파일))
    return sorted(out)


def xml_조(path: Path) -> list[dict]:
    from stage0_extract import extract_xml
    out = []
    for a in extract_xml(path):
        본문 = a.get("본문") or ""
        out.append({"조번호": a["조번호"], "조제목": a.get("조제목") or "", "본문": 본문,
                    "제목키": _제목키(a.get("조제목"), 본문), "본문키": _norm(본문)})
    return out


def xml_영향(conn, 법령doc: str, 조번호: str, 규범문서: set[str]) -> list[dict]:
    """법령 <조> 를 참조하는 우리 규범 조 → 그 조를 근거로 삼는 룰. **깊이 1 만.**

    🔴 조 없는 인용(dst_조번호 IS NULL)은 펴지 않는다 — 근로기준법 하나가
       6,026청크를 끈다 (CLAUDE.md · RAG.md §4-3).

    룰이 안 걸리더라도, 참조하는 쪽이 **우리가 판정에 쓰는 규범 문서**면
    `대상종류='none'` 로 한 줄 남긴다. 룰이 아직 안 쓰인 조일 뿐 사람이 볼 값은 있다.
    """
    srcs = conn.execute("""
        SELECT DISTINCT src_doc_id, src_조번호 FROM corpus.refs
        WHERE dst_doc_id = %s AND dst_조번호 = %s AND src_조번호 IS NOT NULL
    """, (법령doc, 조번호)).fetchall()
    out = []
    for sdoc, s조 in srcs:
        붙은 = _근거조회(conn, sdoc, s조)
        for t in 붙은:
            t = dict(t)
            t["상세"] = {**t["상세"], "경유": f"{sdoc} {s조}"}
            out.append(t)
        if not 붙은 and sdoc in 규범문서:
            out.append({"대상종류": "none", "대상ID": None, "사업명": None, "비목": None,
                        "상세": {"경유": f"{sdoc} {s조}", "비고": "이 조를 근거로 쓰는 룰은 아직 없다"}})
    return out


# ────────────────────────────────────────────────────────────────────
# 5. 적재
# ────────────────────────────────────────────────────────────────────

_필드 = ["종류", "사유코드", "대상종류", "대상ID", "사업명", "비목", "doc_id", "조번호",
         "구doc_id", "구조번호", "변경유형", "유사도", "요약", "상세", "상태"]

# uq_recheck_key 와 같은 열쇠. 여기서 미리 접지 않으면 INSERT 가 서로를 덮어쓰며
# 상세(경유 경로)가 마지막 하나만 남는다.
_열쇠 = ("종류", "사유코드", "대상종류", "대상ID", "doc_id", "조번호", "구doc_id", "구조번호")


def 접기(recs: list[dict]) -> list[dict]:
    """같은 큐 열쇠를 가진 레코드를 하나로 합친다. 상세는 버리지 않고 모은다."""
    묶음: dict[tuple, dict] = {}
    for r in recs:
        k = tuple(r.get(c) for c in _열쇠)
        cur = 묶음.get(k)
        if cur is None:
            묶음[k] = dict(r, 상세=dict(r.get("상세") or {}))
            continue
        경유 = r.get("상세", {}).get("경유")
        if 경유:
            묶음[k]["상세"].setdefault("경유들", [cur["상세"].get("경유")] if cur["상세"].get("경유") else [])
            if 경유 not in 묶음[k]["상세"]["경유들"]:
                묶음[k]["상세"]["경유들"].append(경유)
    return list(묶음.values())


def 적재(conn, recs: list[dict], dry: bool) -> tuple[int, str]:
    """실제 컬럼을 introspect 해 교집합만 넣는다.

    🔴 대소문자를 접어서 맞춘다. `대상ID` 를 따옴표 없이 CREATE 하면 PostgreSQL 이
       `대상id` 로 접는다 — 파이썬 쪽 키와 문자열 비교로는 안 붙는다 (2026-08-31 실측:
       이 한 글자 때문에 대상ID 가 통째로 버려지고 큐가 무엇을 가리키는지 사라졌다).
    """
    실제 = {r[0].lower(): r[0] for r in conn.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema='corpus' AND table_name='recheck_queue'
    """).fetchall()}
    if not 실제:
        FALLBACK.parent.mkdir(parents=True, exist_ok=True)
        FALLBACK.write_text(json.dumps(recs, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0, f"corpus.recheck_queue 없음 (D1-d 대기) → {FALLBACK.relative_to(ROOT)}"

    쓸 = [(f, 실제[f.lower()]) for f in _필드 if f.lower() in 실제]
    빠진 = [f for f in _필드 if f.lower() not in 실제]
    if dry:
        return 0, (f"--dry (적재 안 함). 쓸 컬럼 {len(쓸)}개"
                   + (f" · 버려질 필드 {빠진}" if 빠진 else " · 버려지는 필드 없음"))

    sql = (f"INSERT INTO corpus.recheck_queue ({','.join(chr(34) + c + chr(34) for _, c in 쓸)}) "
           f"VALUES ({','.join('%s' for _ in 쓸)}) ON CONFLICT ON CONSTRAINT uq_recheck_key "
           f"DO UPDATE SET \"요약\" = EXCLUDED.\"요약\", \"상세\" = EXCLUDED.\"상세\", "
           f"\"유사도\" = EXCLUDED.\"유사도\"")
    n = 0
    with conn.cursor() as cur:
        for r in recs:
            vals = []
            for f, _ in 쓸:
                v = r.get("상태", "대기") if f == "상태" else r.get(f)
                vals.append(json.dumps(v, ensure_ascii=False) if f == "상세" else v)
            cur.execute(sql, vals)
            n += cur.rowcount
    conn.commit()
    총 = conn.execute("SELECT count(*) FROM corpus.recheck_queue").fetchone()[0]
    msg = f"corpus.recheck_queue 에 {len(recs)}건 upsert (영향 {n}행) · 표 전체 {총}행"
    return n, msg + (f" · 버려진 필드 {빠진}" if 빠진 else "")


# ────────────────────────────────────────────────────────────────────

# ────────────────────────────────────────────────────────────────────
# 5-b. 큐 치유 — 재적재로 죽은 대상ID 를 정리한다
# ────────────────────────────────────────────────────────────────────

# 대상종류별로 "지금 살아 있는가" 를 묻는 자리. FK 가 없으니 여기서 직접 확인한다.
_생존질의 = {
    "rule": "SELECT 1 FROM corpus.rules WHERE rule_id::text = %s",
    "check_item": 'SELECT 1 FROM corpus.check_items WHERE "code" = %s',
    "precedence_rule": "SELECT 1 FROM corpus.precedence_rules WHERE prec_id::text = %s",
}

# 🔴 재적재 후에도 살아남는 식별자. D 가 recheck_queue 에 FK 를 안 건 이유가 이것이다
#    (`db/init/04_agent.sql` D1-d 주석). 이 조합이 큐의 진짜 열쇠다.
_불변열쇠 = ('"종류"', '"사유코드"', '"대상종류"', '"사업명"', '"비목"',
             "doc_id", '"조번호"', '"구doc_id"', '"구조번호"')


def 치유(conn, dry: bool, 줄) -> None:
    """G 가 `corpus.rules` 를 TRUNCATE RESTART IDENTITY 로 재적재하면 `rule_id` 가
    통째로 갈린다. 큐의 `대상ID` 는 FK 가 아니라 참고값이라 **조용히 죽는다.**

    🔴 이 함수는 반드시 **에이전트를 다시 돌린 뒤에** 부른다. 재실행이 현재 rule_id 로
       살아있는 쌍둥이 행을 먼저 만들어야, 죽은 행을 '중복' 으로 안전하게 지울 수 있다.

    셋으로 가른다:
      ① 죽었는데 **불변열쇠가 같은 살아있는 쌍둥이가 있다** → 지운다 (진짜 중복)
      ② 죽었는데 쌍둥이가 없다 → `상태='기각'` + 사유. **지우지 않는다** —
         근거 자체가 사라진 것이라 사람이 봐야 한다
      ③ 사람이 손댄 행(`처리자` 있음 · `상태<>'대기'`) → 건드리지 않는다. 보고만 한다
    """
    죽음: list[tuple] = []
    for 종류, q in _생존질의.items():
        for qid, 대상id, 상태, 처리자 in conn.execute("""
            SELECT queue_id, "대상id", "상태", "처리자" FROM corpus.recheck_queue
            WHERE "대상종류" = %s AND "대상id" IS NOT NULL
        """, (종류,)).fetchall():
            if not conn.execute(q, (대상id,)).fetchone():
                죽음.append((qid, 종류, 대상id, 상태, 처리자))

    if not 죽음:
        줄("  큐 치유: 죽은 대상ID 없음")
        return
    줄(f"  큐 치유: 죽은 대상ID {len(죽음)}행")

    보호 = [d for d in 죽음 if d[4] is not None or d[3] != "대기"]
    대상 = [d for d in 죽음 if d not in 보호]
    if 보호:
        줄(f"    ③ 사람이 손댄 {len(보호)}행은 건드리지 않는다: "
           f"{[d[0] for d in 보호][:10]}")

    쌍둥이있음, 고아 = [], []
    for qid, *_ in 대상:
        n = conn.execute(f"""
            SELECT 1 FROM corpus.recheck_queue a
            JOIN corpus.recheck_queue b
              ON {' AND '.join(f'b.{c} IS NOT DISTINCT FROM a.{c}' for c in _불변열쇠)}
            WHERE a.queue_id = %s AND b.queue_id <> a.queue_id
              AND b."대상id" IS NOT NULL
              AND EXISTS (SELECT 1 FROM corpus.rules r WHERE r.rule_id::text = b."대상id")
            LIMIT 1
        """, (qid,)).fetchone()
        (쌍둥이있음 if n else 고아).append(qid)

    줄(f"    ① 살아있는 쌍둥이가 있어 지울 것 {len(쌍둥이있음)}행")
    줄(f"    ② 근거가 사라져 '기각' 으로 닫을 것 {len(고아)}행")
    if dry:
        줄("    --dry 라 손대지 않았다")
        return

    if 쌍둥이있음:
        conn.execute("DELETE FROM corpus.recheck_queue WHERE queue_id = ANY(%s)",
                     (쌍둥이있음,))
    if 고아:
        conn.execute("""
            UPDATE corpus.recheck_queue
               SET "상태" = '기각', "처리자" = 'H-재적재정리',
                   "처리일" = current_date,
                   "상세" = "상세" || jsonb_build_object(
                       '무효사유', 'corpus.rules 재적재로 대상 rule_id 가 사라졌고, '
                                  '같은 좌표의 새 룰도 없다. 근거 자체가 없어졌을 수 있다')
             WHERE queue_id = ANY(%s)
        """, (고아,))
    conn.commit()
    남음 = conn.execute(
        "SELECT count(*) FROM corpus.recheck_queue WHERE \"상태\"='대기'").fetchone()[0]
    줄(f"    → 정리 완료. 대기 {남음}행")


def 조정(conn, dry: bool, 줄, 스캔한신doc: set[str] | None = None,
         발행열쇠: set[tuple] | None = None) -> None:
    """이미 해소된 큐 항목을 닫는다.

    큐는 (구판, 신판) diff 에서 **파생된 상태**다. 원본이 바뀌면 파생도 낡는다.
    낡은 채로 두면 큐를 읽는 사람이 이미 고쳐진 것을 또 고치려 한다 —
    2026-09-01 실측: G 가 재도전성공패키지의 현행 자리를 2026년 11차로 고쳤는데
    큐에는 `ACTIVE_NOT_LATEST` 가 그대로 남아 "작년 기준으로 판정 중" 이라고 말하고 있었다.

    🔴 지우지 않고 `상태='반영'` 으로 닫는다. 큐가 무엇을 잡았고 언제 풀렸는지가
       A4 가 실제로 일하는지의 증거라서 지우면 그 이력이 사라진다.
    🔴 사람이 손댄 행은 건드리지 않는다.
    """
    닫을: list[int] = []

    # ① ACTIVE_NOT_LATEST — 그 문서가 더 이상 현행이 아니면 경고가 성립하지 않는다
    for (qid,) in conn.execute("""
        SELECT q.queue_id FROM corpus.recheck_queue q
        WHERE q."사유코드" = 'ACTIVE_NOT_LATEST' AND q."상태" = '대기' AND q."처리자" IS NULL
          AND NOT EXISTS (SELECT 1 FROM corpus.documents d
                          WHERE d.doc_id = q.doc_id AND d."status" = 'active')
    """).fetchall():
        닫을.append(qid)

    # ② 이번 실행에서 같은 신판 문서를 다시 훑었는데 안 나온 항목 → 개정이 사라졌다
    #    🔴 훑지 못한 계보(구판 조문 0건 등)는 판단하지 않는다. 안 나온 게 아니라 못 본 것이다
    if 스캔한신doc and 발행열쇠 is not None:
        for qid, 사유, doc, 조, 구doc, 구조 in conn.execute("""
            SELECT queue_id, "사유코드", doc_id, "조번호", "구doc_id", "구조번호"
            FROM corpus.recheck_queue
            WHERE "종류" = 'A4개정' AND "상태" = '대기' AND "처리자" IS NULL
              AND "사유코드" <> 'ACTIVE_NOT_LATEST' AND doc_id = ANY(%s)
        """, (list(스캔한신doc),)).fetchall():
            if (사유, doc, 조, 구doc, 구조) not in 발행열쇠:
                닫을.append(qid)

    if not 닫을:
        줄("  큐 조정: 해소된 항목 없음")
        return
    줄(f"  큐 조정: 해소된 항목 {len(set(닫을))}행 → '반영' 으로 닫는다")
    if dry:
        줄("    --dry 라 손대지 않았다")
        return
    conn.execute("""
        UPDATE corpus.recheck_queue
           SET "상태" = '반영', "처리자" = 'H-A4자동조정', "처리일" = current_date,
               "상세" = "상세" || jsonb_build_object(
                   '해소', '재실행에서 더 이상 감지되지 않는다. 원본이 고쳐졌거나 근거가 바뀌었다')
         WHERE queue_id = ANY(%s)
    """, (list(set(닫을)),))
    conn.commit()


def 실행_db(conn, 줄, 스캔한신doc: set[str] | None = None) -> list[dict]:
    recs: list[dict] = []
    for g in 계보(conn):
        줄(f"\n■ {g['키']}")
        for p in g["판들"]:
            표 = "◀ 현행" if p["doc_id"] == g["신"]["doc_id"] else ""
            줄(f"    {p['status']:<11} {p['조문수']:>3}조  {p['doc_id'][:62]} {표}")
        for 코드, 최신 in g["경고"]:
            줄(f"    🔴 {코드} — 현행 자리가 {g['신']['doc_id'][:40]} 인데 "
               f"더 최근 판 {최신[:40]} 이 superseded 다")
            recs.append({
                "종류": "A4개정", "사유코드": "ACTIVE_NOT_LATEST",
                "대상종류": "none", "대상ID": None, "사업명": None, "비목": None,
                "doc_id": g["신"]["doc_id"], "조번호": None,
                "구doc_id": 최신, "구조번호": None,
                "변경유형": None, "유사도": None,
                "요약": f"더 최근 판({최신})이 superseded 이고 구판이 현행 자리를 차지하고 있다. "
                        f"그대로 두면 지난 기준으로 판정한다.",
                "상세": {"판들": [p["doc_id"] for p in g["판들"]]},
            })
        if not g["구"]:
            줄("    (구판 조문이 없어 diff 생략)")
            continue

        if 스캔한신doc is not None:
            스캔한신doc.add(g["신"]["doc_id"])      # diff 를 실제로 돌린 것만 담는다
        구, 신 = 조_읽기(conn, g["구"]["doc_id"]), 조_읽기(conn, g["신"]["doc_id"])
        쌍들 = 조_매칭(구, 신)
        변경조, 통계 = [], {}
        for 쌍 in 쌍들:
            변경 = 판정_변경(쌍)
            통계[변경["변경유형"]] = 통계.get(변경["변경유형"], 0) + 1
            if 변경["변경유형"] == "동일":
                continue
            변경조.append((쌍, 변경))
            recs += 영향_레코드(conn, g["신"]["doc_id"], g["구"]["doc_id"], 쌍, 변경)
        근거통계 = {}
        for 쌍 in 쌍들:
            근거통계[쌍["근거"].split("0.")[0]] = 근거통계.get(쌍["근거"].split("0.")[0], 0) + 1
        줄(f"    구 {len(구)}조 ↔ 신 {len(신)}조   매칭 {근거통계}")
        줄(f"    변경 {통계}")
        recs += 어휘집_트리거(변경조, g["신"]["doc_id"], g["구"]["doc_id"])
    return recs


def 실행_xml(conn, 줄, 한도: int | None) -> list[dict]:
    쌍목록 = xml_계보()
    줄(f"\n■ 법령 연혁 XML — 현행 대비 직전 판이 있는 법령 {len(쌍목록)}건")
    # 우리 코퍼스가 실제로 참조하는 법령만 본다. 944건 전부 파싱하면 수분이 날아가고,
    # 아무도 인용하지 않는 법령의 개정은 recheck 대상이 아니다.
    참조되는 = {r[0] for r in conn.execute("""
        SELECT DISTINCT dst_doc_id FROM corpus.refs
        WHERE dst_doc_id IS NOT NULL AND dst_조번호 IS NOT NULL
    """).fetchall()}
    대상 = [x for x in 쌍목록 if x[0] in 참조되는]
    줄(f"    그중 corpus.refs 가 조 지정으로 참조하는 것 {len(대상)}건만 본다")
    if 한도:
        대상 = 대상[:한도]

    # 판정에 실제로 쓰는 규범 문서 = rules.근거 가 가리키는 문서들
    규범문서 = {r[0] for r in conn.execute("""
        SELECT DISTINCT e->>'doc_id' FROM corpus.rules, jsonb_array_elements("근거") e
    """).fetchall()}

    recs: list[dict] = []
    전체변경 = 참조된변경 = 0
    for doc_id, 구파일, 신파일 in 대상:
        try:
            구, 신 = xml_조(구파일), xml_조(신파일)
        except Exception as e:                       # noqa: BLE001
            줄(f"    ✗ {doc_id[:50]} 파싱 실패 {type(e).__name__}")
            continue
        if not 구 or not 신:
            continue
        변경수 = 0
        for 쌍 in 조_매칭(구, 신):
            변경 = 판정_변경(쌍)
            if 변경["변경유형"] == "동일" or not 쌍["신"]:
                continue
            전체변경 += 1
            대상들 = xml_영향(conn, doc_id, 쌍["신"]["조번호"], 규범문서)
            if not 대상들:
                continue
            참조된변경 += 1
            변경수 += 1
            for t in 대상들:
                recs.append({
                    "종류": "A4개정", "사유코드": "UPSTREAM_LAW_AMENDED",
                    "대상종류": t["대상종류"], "대상ID": t["대상ID"],
                    "사업명": t.get("사업명"), "비목": t.get("비목"),
                    "doc_id": doc_id, "조번호": 쌍["신"]["조번호"],
                    "구doc_id": 구파일.stem, "구조번호": 쌍["구"]["조번호"] if 쌍["구"] else None,
                    "변경유형": 변경["변경유형"], "유사도": 변경["유사도"],
                    "요약": 변경["요약"], "상세": {**t["상세"], "매칭근거": 쌍["근거"]},
                })
        if 변경수:
            줄(f"    · {doc_id[:56]}  영향 있는 개정 조 {변경수}개")
    # 🔴 0건이 나와도 '고장' 이 아니다. 아래 두 숫자가 그걸 갈라준다.
    줄(f"    개정된 조 {전체변경}개 중 우리 규범이 조 지정으로 참조하는 것 {참조된변경}개")
    return recs


def main() -> None:
    ap = argparse.ArgumentParser(description="A4 개정 대응 — 변경 조 → 영향 룰 → recheck_queue")
    ap.add_argument("--source", choices=["db", "xml", "both"], default="db")
    ap.add_argument("--dry", action="store_true", help="적재하지 않고 보고만")
    ap.add_argument("--xml-limit", type=int, help="XML 경로에서 앞 N개 법령만")
    ap.add_argument("--heal", action="store_true",
                    help="적재 후 죽은 대상ID 정리 (G 의 rules 재적재 뒤에 쓴다)")
    ap.add_argument("--heal-only", action="store_true", help="감지 없이 치유만")
    ap.add_argument("--report", default=str(REPORT))
    a = ap.parse_args()

    if a.heal_only:
        with psycopg.connect(DSN) as conn:
            치유(conn, a.dry, print)
            조정(conn, a.dry, print)      # 열쇠 대조 없이 ACTIVE_NOT_LATEST 만
        return

    줄들: list[str] = []

    def 줄(s=""):
        print(s, flush=True)
        줄들.append(s)

    줄("A4 개정 대응 — 조 매칭(제목 주·번호 보조) → 본문 diff → 근거 역조회 → recheck_queue")
    with psycopg.connect(DSN) as conn:
        recs: list[dict] = []
        스캔한신doc: set[str] = set()
        if a.source in ("db", "both"):
            recs += 실행_db(conn, 줄, 스캔한신doc)
        if a.source in ("xml", "both"):
            recs += 실행_xml(conn, 줄, a.xml_limit)

        줄("\n" + "=" * 74)
        원래 = len(recs)
        recs = 접기(recs)
        if 원래 != len(recs):
            줄(f"큐 열쇠 중복 접기: {원래} → {len(recs)}건 (상세.경유들 로 보존)")
        집계: dict[str, int] = {}
        for r in recs:
            집계[r["사유코드"]] = 집계.get(r["사유코드"], 0) + 1
        줄(f"큐 후보 {len(recs)}건  {집계 or '{}'}")
        for r in recs[:25]:
            줄(f"  [{r['사유코드']}] {r['대상종류']}#{r['대상ID']} "
               f"{r.get('사업명') or ''} {r.get('비목') or ''} "
               f"{r.get('doc_id') or ''} {r.get('조번호') or ''}".rstrip())
        if len(recs) > 25:
            줄(f"  … 외 {len(recs) - 25}건")

        n, msg = 적재(conn, recs, a.dry)
        줄(f"\n적재: {msg}")
        if a.heal:
            치유(conn, a.dry, 줄)
            # 🔴 db 경로를 안 돌렸으면 열쇠 대조를 하지 않는다 — 못 본 것과 없어진 것은 다르다
            열쇠 = {(r["사유코드"], r.get("doc_id"), r.get("조번호"),
                     r.get("구doc_id"), r.get("구조번호")) for r in recs}                 if a.source in ("db", "both") else None
            조정(conn, a.dry, 줄, 스캔한신doc or None, 열쇠)

    Path(a.report).parent.mkdir(parents=True, exist_ok=True)
    Path(a.report).write_text(
        "# A4 개정 대응 실행 보고\n\n> `scripts/agent_a4.py` 산출. 재현성을 위해 LLM 을 쓰지 않는다.\n\n"
        "```\n" + "\n".join(줄들) + "\n```\n", encoding="utf-8")
    print(f"\n보고서 → {a.report}")


if __name__ == "__main__":
    main()
