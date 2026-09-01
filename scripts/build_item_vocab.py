# -*- coding: utf-8 -*-
"""Stage 1-a : 비목 용어 사전 -> `_비목_어휘집.json`.

`rule_base.md` §1-b 의 산출물. **canonical enum 의 기준 문서**이다.

(1) 정규화 출력의 `비목후보`, `rules.비목`, `item_alias.비목`, 정답셋의 비목이
같은 폐쇄 목록을 쓰지 않으면 시스템이 닫히지 않는다. `guided_json` enum 의 원천이기도 하다.

2층 + 1축이다.

    기간(基幹)   L1 통합관리지침 제37~45조의 법정 비목       전 사업 공통
    사업별 확장  L2 세부관리기준 비목 해설표                 사업 스코프 한정
    적용대상 축  {창업기업, 주관기관}                        아키텍처 §2-2

🔴 **적용대상 축이 enum 의 일부다.** 우리 사용자는 창업기업이다. 주관기관 비목
(일반수용비·회의비·사업운영비 등)이 enum 에 섞이면 "노트북 사도 되나요?" 에
`일반수용비` 가 비목후보로 뜬다. 세부관리기준은 두 비목표를 나란히 싣고 있고
(붙임1=주관기관 / 붙임2=창업기업등), **섹션 조제목이 대상을 명시**하므로 그대로 읽는다.

**별칭은 여기 넣지 않는다.** 맥북 -> 기계장치 매핑은 `item_alias` 의 몫이다.
용어 사전은 목적지 목록일 뿐이다.

실행:
    python scripts/build_item_vocab.py
    python scripts/build_item_vocab.py --check   파일 안 쓰고 대조만
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "2026_Finance_DATA_FOR_RAG"
OUT = DATA / "_비목_어휘집.json"

ART = DATA / "_stage0_articles.json"
TBL = DATA / "_tables.json"
SHARED = ROOT / "공유받은 파일" / "비목명 종합.csv"

지침_DOC = "L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223"

# 🔴 제36조는 `창업기업등 사업비 비목` — 비목이 아니라 **총칙 조**다 (본문 2,467자).
# 개별 법정 비목은 제37조(재료비)~제45조(광고선전비) 9개. rule_base.md §1-b 와 일치한다.
기간_조범위 = range(37, 46)

사업_키워드 = [
    ("예비창업", "예비창업패키지"), ("초기창업", "초기창업패키지"),
    ("재도전", "재도전성공패키지"), ("창업도약", "창업도약패키지"),
    ("창업중심대학", "창업중심대학"), ("초격차", "초격차 스타트업 프로젝트"),
    ("모두의", "모두의 창업 프로젝트"), ("팁스", "민관공동창업자발굴육성(TIPS)"),
    ("TIPS", "민관공동창업자발굴육성(TIPS)"),
]

# ── 조판 사고 수습 ───────────────────────────────────────────────
# 자동 규칙(접두/접미 흡수)을 만들면 `자산취득비`(창업중심대학)까지 삼킨다.
# 파편 재결합은 일반 규칙이 아니라 **개별 사고**라 명시 맵으로 둔다 — 근거를 적어둔다.
# 맵의 좌변이 실제로 안 나오면 경고한다 (재파싱으로 사고가 사라지면 맵도 지워야 한다).
파편_병합 = {
    # 초기창업 2024 [붙임2]: 셀 경계에서 `특허권 등 무형자산` / `취득비` 로 갈렸다.
    # 같은 표 2025년판에 온전한 `특허권 등 무형자산 취득비` 가 있어 교차 확인된다.
    ("초기창업패키지 세부관리기준(2024년)", "특허권등무형자산"): "특허권등무형자산취득비",
    ("초기창업패키지 세부관리기준(2024년)", "취득비"): "특허권등무형자산취득비",
}
# 한 셀에 두 비목이 묶인 경우. 같은 문서 다른 별지에 각각 따로 나온다.
셀_분할 = {
    "재료비기계장치": ["재료비", "기계장치"],
}

머리글 = {"비목", "구분", "구 분", "세목", "내용", "정의", "증빙 서류", "증빙서류",
          "유의 사항", "유의사항", "항목", "합계", "소계", "비 목", "세 목",
          "기타", "기 타", "공통", "공 통", "주요내용", "주요 내용", "비고",
          "세세목", "세 세 목"}
RE_비목아님 = re.compile(r"^(기\s*타|공\s*통|주요\s*내용|비\s*고|소\s*계|합\s*계|세\s*세\s*목)")

# 표 안에 표가 들어 있으면 조판이 한 셀로 뭉갠다. 그런 셀은 비목이 아니라 표 조각이다.
RE_표조각 = re.compile(r"[①-⑮]|증빙|서류|영수증|계약서")


def norm(name: str) -> str:
    """표기 흔들림을 하나로 모은다.

    조판이 셀 안에서 단어를 가른다 (`기계장치 (공구·기 구...)`, `창업 활동비`).
    세목 코드가 붙는다 (`임차료(07)`). 법정 명칭에 열거가 붙는다 (`기계장치, 공구·기구`).
    셋 다 같은 비목이므로 **괄호·콤마 이하를 자르고 공백·가운뎃점을 지운다.**
    """
    s = re.sub(r"\s+", "", name or "")
    s = re.sub(r"[(（].*$", "", s)      # 괄호 이하 절단 — 세목 코드·부연 열거
    s = re.sub(r"[,、].*$", "", s)      # 콤마 이하 절단 — `기계장치, 공구·기구`
    return s.replace("·", "").replace("‧", "").replace("․", "")


def 사업_of(doc_id: str, sec_title: str = "") -> str | None:
    """사업명. 범위 밖이면 None (호출부가 버린다).

    🔴 **모두의창업은 일반·기술트랙만 다룬다** (`CLAUDE.md` 사업 스코프).
    세부관리기준이 제1편 총칙 / 제2편 일반·기술트랙 / 제3편 로컬트랙 구조인데
    로컬트랙은 상위 규범이 통합관리지침이 아니라 「신사업창업사관학교 운영지침」이라
    위임 계통이 다르다. 비목 체계도 갈린다 — 일반·기술트랙(별지1·2)은 기간 비목 계열,
    로컬트랙(별지4)은 회계 세목 코드 계열(`임차료(07)`·`무형자산(01)`)이다.
    **로컬트랙 비목을 남겨두면 일반·기술트랙 판정에 범위 밖 후보가 뜬다** —
    `build_precedence.스코프_컷` 과 같은 이유로 여기서 버린다.
    """
    for k, v in 사업_키워드:
        if k not in doc_id:
            continue
        if v == "모두의 창업 프로젝트" and "로컬" in sec_title:
            return None
        return v
    return None


def 대상_of(조제목: str, 본문머리: str) -> str:
    """섹션이 누구 비목표인가. **조제목이 기준 문서**, 없을 때만 본문 머리를 본다.

    본문을 먼저 보면 안 된다 — 주관기관 표 본문에도 `유망창업기업 프로그램` 같은
    말이 나와서 창업기업으로 오판한다.
    """
    for src in (조제목 or "", 본문머리 or ""):
        if "창업기업" in src or "진출자" in src:
            return "창업기업"
        if "주관기관" in src or "운영기관" in src:
            return "주관기관"
    return "미상"


# ── 1층: 통합관리지침 법정 비목 ──────────────────────────────────
def 기간어휘(arts: dict) -> list[dict]:
    doc = arts.get(지침_DOC)
    if not doc:
        print(f"!! 지침 문서를 못 찾았다: {지침_DOC}")
        return []
    out = []
    for a in doc["articles"]:
        n = a.get("조번호_int")
        if n is None or n not in 기간_조범위 or a.get("삭제"):
            continue
        title = (a.get("조제목") or "").strip()
        if not title:
            continue
        out.append({
            "비목": title, "정규": norm(title), "층": "기간",
            # 지침 제36조가 "창업기업등 사업비 비목" 이므로 제37~45조는 전부 창업기업 축이다.
            "적용대상": "창업기업", "사업스코프": [],
            "근거": {"doc_id": 지침_DOC, "조번호": a["조번호"]},
        })
    return out


# ── 2층: L2 비목 해설표 ─────────────────────────────────────────
def 섹션정보(arts: dict) -> dict:
    """(doc_id, 섹션) -> {조제목, 본문머리}. 적용대상 판정의 근거."""
    info = {}
    for doc_id, d in arts.items():
        for a in d["articles"]:
            info[(doc_id, a["조번호"])] = {
                "조제목": a.get("조제목") or "",
                "본문머리": (a.get("본문") or "").replace("\n", " ")[:120],
            }
    return info


def is_비목표(t: dict, sec_title: str) -> bool:
    """이 표가 **비목 정의표**인가. 증빙표가 아니라 정의표여야 한다.

    🔴 섹션이 `붙임N`/`참고N` 이라는 것만으로는 안 된다. 같은 섹션에 서식·점검표가
    섞여 있어서, 1열을 무조건 비목으로 삼으면 `대표자명`·`연번` 같은 **서식 필드명이
    비목이 된다** (실측: 그렇게 뽑으면 208개).

    반대로 `비목+정의+증빙` 3종 동시 출현을 요구하면 **너무 좁다** — 창업중심대학·
    초격차는 정의표와 증빙표를 나눠 실어서 창업기업 비목이 통째로 빠진다.

    그렇다고 `정의 or 증빙` 으로 풀면 **증빙표가 들어온다.** 증빙표는 셀 안에
    세목 하위표를 통째로 품고 있어서 (`비목 증빙서류 ① 계약서 기술보호비 ②…`)
    `멘토링비`·`학회` 같은 **세목이 비목으로 승격된다** (실측: 창업중심대학 [참고4]
    에서 16개 세목이 샜다).

    정의표와 증빙표를 가르는 것은 **머리글에 `정의` 가 있는가**다:
        [참고3] `비목|세목|세세목|정의`      -> 정의표  O
        [참고4] `비목|집행 증빙서류`          -> 증빙표  X
    머리글이 병합셀로 비어 있는 판본(예비창업 붙임2 `비목||내용`)만 셀을 본다.
    """
    if not t["행"]:
        return False
    head = " ".join(t["행"][0])
    if "비목" not in head:
        return False
    if "정의" in head:
        return True
    flat = " ".join(c for r in t["행"] for c in r)
    return "정의" in flat and "증빙" in flat


def 사업확장(tables: dict, secinfo: dict) -> tuple[list[dict], list[str]]:
    out, 미상 = [], []
    for t in tables.get("tables", []):
        doc_id, sec = t["doc_id"], t.get("섹션") or ""
        si = secinfo.get((doc_id, sec), {})
        if not is_비목표(t, si.get("조제목", "")):
            continue
        biz = 사업_of(doc_id, si.get("조제목", ""))
        if not biz:
            continue
        대상 = 대상_of(si.get("조제목", ""), si.get("본문머리", ""))
        if 대상 == "미상":
            미상.append(f"{doc_id} / {sec} / {si.get('조제목', '')[:40]}")
        head = t["행"][0]
        col = next((i for i, c in enumerate(head) if "비목" in c), 0)
        for r in t["행"][1:]:
            if col >= len(r):
                continue
            cell = (r[col] or "").strip()
            if not cell or cell in 머리글 or RE_비목아님.match(cell):
                continue
            if len(cell) > 30 or len(cell) < 2 or RE_표조각.search(cell):
                continue
            키 = norm(cell)
            키 = 파편_병합.get((doc_id, 키), 키)
            for 조각 in 셀_분할.get(키, [키]):
                out.append({
                    "비목": cell, "정규": 조각, "층": "사업별",
                    "적용대상": 대상, "사업스코프": [biz],
                    "근거": {"doc_id": doc_id, "조번호": sec or "부속표"},
                })
    return out, 미상


def 병합(rows: list[dict]) -> list[dict]:
    """정규명 + 적용대상이 같으면 한 항목.

    적용대상을 키에 넣는 이유: `인건비`·`여비`·`지급수수료` 는 주관기관 표와 창업기업
    표에 **둘 다** 나오지만 집행 기준이 다르다. 하나로 합치면 룰 조회 키가 무너진다.
    """
    by: dict[tuple[str, str], dict] = {}
    for r in rows:
        k = (r["정규"], r["적용대상"])
        if k not in by:
            by[k] = {"enum": r["정규"], "비목": r["비목"], "층": r["층"],
                     "적용대상": r["적용대상"], "사업스코프": [], "근거": [],
                     "표기변형": set()}
        e = by[k]
        e["표기변형"].add(r["비목"])
        if r["층"] == "기간":
            e["층"] = "기간"
            e["비목"] = r["비목"]                # 법정 명칭을 대표로
        for s in r["사업스코프"]:
            if s not in e["사업스코프"]:
                e["사업스코프"].append(s)
        if r["근거"] not in e["근거"]:
            e["근거"].append(r["근거"])
    out = []
    for e in by.values():
        e["표기변형"] = sorted(e["표기변형"])
        e["사업스코프"] = [] if e["층"] == "기간" else sorted(e["사업스코프"])
        out.append(e)
    return sorted(out, key=lambda x: (x["적용대상"] != "창업기업",
                                      x["층"] != "기간", x["enum"]))


def 대조(vocab: list[dict]) -> dict:
    """팀 반입 종합표와 대조해 결손을 드러낸다.

    종합표는 21개 판본을 사람이 대조한 2차 편집물이라 **판정 근거로는 못 쓴다**
    (근거 조번호가 없다). 커버리지 체크리스트로만 쓴다.
    """
    if not SHARED.exists():
        return {"상태": "공유받은 파일 없음"}
    raw = SHARED.read_bytes()
    txt = ""
    for enc in ("utf-8-sig", "cp949", "utf-8"):
        try:
            txt = raw.decode(enc)
            break
        except Exception:  # noqa: BLE001
            continue
    rows = list(csv.DictReader(io.StringIO(txt)))
    ours = {v["enum"] for v in vocab}
    theirs: dict[str, set] = {}
    for r in rows:
        nm = (r.get("비목 이름") or "").strip()
        if not nm:
            continue
        base = nm.split(" - ")[0].split("-")[0].strip()
        theirs.setdefault(norm(base), set()).add(nm)
    return {
        "종합표_행": len(rows), "종합표_비목축": len(theirs), "우리_비목": len(ours),
        "우리에게_없음": sorted(k for k in theirs if k not in ours),
        "종합표에_없음": sorted(k for k in ours if k not in theirs),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    arts = json.loads(ART.read_text(encoding="utf-8")) if ART.exists() else {}
    tables = json.loads(TBL.read_text(encoding="utf-8")) if TBL.exists() else {}

    secinfo = 섹션정보(arts)
    base = 기간어휘(arts)
    ext, 미상 = 사업확장(tables, secinfo)
    vocab = 병합(base + ext)

    적용 = {(d, k) for d, k in 파편_병합}
    본 = {(r["근거"]["doc_id"], norm(r["비목"])) for r in ext}
    미적용 = [f"{d}/{k}" for d, k in 적용 if (d, k) not in 본 and k not in
              {v["enum"] for v in vocab}]

    # 교차확인: 근거 판본이 2개 이상인가. 한 판본의 표 한 칸만 근거인 비목은
    # 조판 사고와 구분이 안 된다 — 실제로 초격차 구판 [참고5] 한 곳에서만 나오는
    # `업무추진비`·`운영비` 는 현행 2024·2025 판본에서는 주관기관 비목이다.
    # `rule_base.md` §6 의 "verified=false 룰 단독으로 가능 판정 금지" 와 같은 취지.
    for v in vocab:
        v["교차확인"] = v["층"] == "기간" or len(v["근거"]) >= 2
        if not v["교차확인"]:
            v["검수필요"] = "근거 판본 1개 — 조판 사고와 구분 불가. 원문 확인 후 승격"

    창업 = [v for v in vocab if v["적용대상"] == "창업기업"]
    enum = [v["enum"] for v in 창업 if v["교차확인"]]
    대기 = [v["enum"] for v in 창업 if not v["교차확인"]]

    doc = {
        "생성": "scripts/build_item_vocab.py",
        "사양": "rule_base.md §1-b",
        "코퍼스_스냅샷": {
            "문서수": len(arts),
            "표수": len(tables.get("tables", [])),
            "주의": ("이 어휘집은 위 코퍼스에서 뽑은 것이다. L2 세부관리기준이 늘거나 "
                     "Stage 0 를 다시 돌리면 재실행해야 한다. L1 법령만 늘어난 경우는 "
                     "기간 어휘(통합관리지침 제37~45조)가 그대로면 영향 없다."),
        },
        "주의": ("`층=기간` 은 전 사업 공통(사업스코프 빈 배열). `층=사업별` 은 해당 사업만. "
                 "🔴 `적용대상` 이 enum 의 일부다 — 우리 사용자는 창업기업이므로 "
                 "guided_json enum 은 `적용대상=창업기업` 만 쓴다. 주관기관 비목은 "
                 "아키텍처 §2-2 음성 대조군용으로만 보관한다. "
                 "별칭(맥북->기계장치)은 여기가 아니라 item_alias 의 몫이다."),
        "요약": {
            "총": len(vocab),
            "창업기업": len(창업),
            "주관기관": sum(1 for v in vocab if v["적용대상"] == "주관기관"),
            "미상": sum(1 for v in vocab if v["적용대상"] == "미상"),
            "기간": sum(1 for v in vocab if v["층"] == "기간"),
            "사업별": sum(1 for v in vocab if v["층"] == "사업별"),
            "enum_확정": len(enum),
            "enum_검수대기": len(대기),
        },
        "guided_json_enum": enum,
        "enum_검수대기": 대기,
        "적용대상_미상_섹션": sorted(set(미상)),
        "파편병합_미적용": 미적용,
        "커버리지_대조": 대조(vocab),
        "vocab": vocab,
    }
    if not args.check:
        OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    s = doc["요약"]
    print(f"비목 {s['총']}  (창업기업 {s['창업기업']} / 주관기관 {s['주관기관']} / 미상 {s['미상']})")
    print(f"          (기간 {s['기간']} / 사업별 {s['사업별']})\n")
    for v in vocab:
        scope = ",".join(v["사업스코프"]) or "전사업"
        mark = "  " if v["교차확인"] else "🟡"
        print(f"  {mark}[{v['적용대상']:<4}|{v['층']:<3}] {v['enum'][:20]:<22} "
              f"{scope[:44]:<46} 근거{len(v['근거']):>2}")
    if 미상:
        print("\n!! 적용대상 미상 섹션:")
        for m in sorted(set(미상)):
            print(f"   {m}")
    if 미적용:
        print(f"\n!! 파편병합 맵이 안 걸렸다 (재파싱으로 사고가 사라졌나?): {미적용}")
    print(f"\nguided_json enum (창업기업 · 교차확인 {len(enum)}):")
    print("  " + ", ".join(enum))
    if 대기:
        print(f"\n🟡 검수대기 {len(대기)} (근거 판본 1개 — enum 미투입):")
        print("  " + ", ".join(대기))
    chk = doc["커버리지_대조"]
    print("\n커버리지 대조:", json.dumps({k: v for k, v in chk.items()
                                       if k != "우리에게_없음"}, ensure_ascii=False))
    if chk.get("우리에게_없음"):
        print("  원문에서 못 뽑은 비목:", ", ".join(chk["우리에게_없음"][:30]))
    if not args.check:
        print(f"\n-> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
