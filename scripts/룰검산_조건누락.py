# -*- coding: utf-8 -*-
"""corpus.rules 전수에서 「근거 조문의 제약을 룰이 빠뜨렸는가」를 기계로 검산한다.

배경 (레인 K, 2026-09-06 · Handoff_인수인계_조건누락_검산기.md):
    사람(P3-a)이 28행을 손으로 읽어 조건누락 6건(431·441·427·437·464·435)을 찾았다.
    나머지 54행(82-28)은 아직 아무도 안 봤다 — 「몇 건 중 6건인지」를 모른다.
    이 스크립트는 그 답을 내려고 만든다. **재현이 관문이다**: 손으로 찾은 6건을
    다시 찾아내지 못하면 나머지 결과를 믿을 수 없다 (아래 검출력() 참고).

🔴 어미 목록은 중앙이 준 시작점("집행할 수 없다" 등 «금지» 어미)에서 실측으로 늘렸다.
    실측(scripts 실행 로그, 2026-09-06): 431·441·435 셋은 재현이 «금지» 어미만으론 안 됐다.
    이유: 그 세 건의 원문은 "~하여야 하며"(의무) · "~원칙으로 함"(원칙) 이지
    "~할 수 없다"(금지) 가 «아니다». 「조건누락」은 금지뿐 아니라 의무·원칙 누락도
    포함한다는 게 P3-a 의 실제 정의였다 — 그래서 어미를 금지형+의무형 둘 다로 늘렸다.
    이 확장 없이 돌리면 재현율이 3/6에서 멈춘다(위약금·금형·재하청만 잡힘).

🔴 붙임2(해설표)는 조문과 다르게 "다."로 안 끝나고 "•" 불릿으로 나뉜다.
    "다." 로만 문장을 자르면 붙임2 안의 문장(431·441·435 근거가 바로 여기 있다)이
    통째로 한 덩어리가 되어 끝-어미 검사에 안 걸린다. 그래서 "•" 로도 따로 자른다.

매칭은 "느슨한" 문자 바이그램 커버리지다 — 임계는 알려진 6건으로 스스로 정했다
(아래 __main__ 의 검출력() 출력 참고). 정확한 형태소 매칭이 아니라 «후보 스크리닝» 이다.

실행:
    PYTHONIOENCODING=utf-8 python scripts/룰검산_조건누락.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import db  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "scratchpad"

# ────────────────────────────────────────────────────────────────────
# 어미 목록 — 실측으로 정함 (금지형: 중앙 시작점 + 코퍼스 실측 / 의무형: 재현 실패로 추가)
# ────────────────────────────────────────────────────────────────────

# 금지형: "~할 수 없다" 류. 46개 근거 조문 전문에서 문장(마침표 "다." 기준) 끝 20자를
# 돌려 실측(2026-09-06) — 61/433 문장이 여기 걸렸고 육안 확인상 오탐 없음.
금지_어미 = re.compile(
    r"(수\s*없다|불가하다|불가$|아니\s*된다|아니된다|안\s*된다|못한다|"
    r"인정하지\s*(아니한다|않는다)|제외한다)\.?$"
)

# 의무·원칙형: "~하여야 한다" 류. 431·441·435 재현 실패 뒤 추가 — 같은 방식으로 실측(86/433).
# 🔴 "~할 수 있다"(허용) 와 안 갈리게, "~한하여"·"~한한다" 처럼 «범위를 좁히는» 것만 넣는다.
#    "~하여야 한다" 자체는 허용/금지 방향이 없어 오탐 위험이 있다 — 그래서 [2단계] 바이그램
#    커버리지로 한 번 더 거른다. 여기선 "후보"만 넓게 잡는다.
의무_어미 = re.compile(
    r"(하여야\s*한다|하여야\s*함|되어야\s*한다|준용하여야|원칙으로\s*한다|원칙으로\s*함|"
    r"따라야\s*한다|한하여|경우에\s*한한다|이내에\s*한하여)\.?$"
)

제약_어미 = re.compile(f"({금지_어미.pattern[:-1]})|({의무_어미.pattern[:-1]})\\.?$")


# ────────────────────────────────────────────────────────────────────
# 🔴 다중-비목 표 문서 스코핑 — 611건 오염의 원인을 잡는다
# ────────────────────────────────────────────────────────────────────
# 붙임2(예비·초기)·참고2(초격차)·참고3/4(창업중심대학)는 «한 조문 안에 비목 10개가
# 전부» 들어있다. 조번호 하나로 근거를 걸면(현재 corpus.rules 구조가 그렇다)
# 지급수수료 룰(431)을 검사하면서 그 문서 안의 «인건비» 문단까지 통째로 딸려온다
# (실측: 이 스코핑 없이 82행 돌리면 누락후보 611건 — 재현 6/6은 유지되지만 노이즈가
# 압도적이다. `lane-metrics-cross-contaminate` 함정과 같은 모양).
# 문서 이름으로 판별하지 않는다 — 표 안에 «알려진 비목명이 표 첫 칸에 2개 이상»
# 등장하는지 직접 세어서, 그럴 때만 비목별로 자른다.
비목_목록 = ["재료비", "외주용역비", "기계장치", "특허권등무형자산취득비", "인건비",
           "지급수수료", "여비", "교육훈련비", "광고선전비", "창업활동비"]


def _정규화_비목(s: str) -> str:
    s = re.sub(r"\(.*?\)", "", s)
    s = re.sub(r"[\s·,]", "", s)
    return s.replace("등", "")


_비목_정규화_목록 = [_정규화_비목(x) for x in 비목_목록]


def 비목별_섹션(본문: str) -> dict[str, str] | None:
    """표 첫 칸에 등장하는 비목명을 경계로 본문을 자른다. 다중비목 표가 아니면 None."""
    lines = 본문.split("\n")
    경계 = []
    for i, line in enumerate(lines):
        if not line.strip().startswith("|"):
            continue
        cells = line.split("|")
        if len(cells) < 2:
            continue
        first = cells[1].strip()
        if not first:
            continue
        norm = _정규화_비목(first)
        for j, known in enumerate(_비목_정규화_목록):
            if norm.startswith(known) or (known and known.startswith(norm) and len(norm) >= 2):
                경계.append((i, 비목_목록[j]))
                break
    라벨수 = len({label for _, label in 경계})
    if 라벨수 < 2:
        return None  # 다중비목 표가 아니다 — 단일 조문 그대로 쓴다
    섹션: dict[str, list[str]] = {}
    for k, (i, label) in enumerate(경계):
        end = 경계[k + 1][0] if k + 1 < len(경계) else len(lines)
        섹션.setdefault(label, []).append("\n".join(lines[i:end]))
    return {label: "\n".join(chunks) for label, chunks in 섹션.items()}


# 🔴 두 번째 오염원 — 표가 아니라 «번호목록» 으로 여러 비목을 한 조문에 나열하는 경우.
#    실물: 예비 제22조/초기 제21조 — "1. 외주용역비 ... 2. 여비 ... 3. 기계장치 ... 4. 여비"
#    (표 스코핑만으론 안 잡힌다 — "|" 가 없다). 항목이 «어느 비목 얘기인지» 는 동의어로 잡는다.
_비목_동의어 = {
    "외주용역비": ["외주용역비", "외주 용역비"],
    "여비": ["여비", "국외 출장"],
    "기계장치": ["기계장치", "기계, 장비 등 취득자산", "기계, 장비"],
    "재료비": ["재료비"],
    "인건비": ["인건비"],
    "지급수수료": ["지급수수료", "지급 수수료"],
    "교육훈련비": ["교육훈련비", "교육 훈련비"],
    "광고선전비": ["광고선전비", "광고 선전비"],
    "특허권등무형자산취득비": ["특허권", "무형자산 취득비"],
    "창업활동비": ["창업활동비", "창업 활동비"],
}


def 번호목록_섹션(본문: str) -> dict[str, str] | None:
    조각 = re.split(r"\n(?=\d+\.\s)", 본문)
    if len(조각) < 2:
        return None
    태그됨: dict[str, list[str]] = {}
    for chunk in 조각[1:]:
        matches = sorted({biz for biz, syns in _비목_동의어.items() if any(s in chunk for s in syns)})
        if len(matches) == 1:
            태그됨.setdefault(matches[0], []).append(chunk)
        # 0개(무관) 또는 2개 이상(모호)인 항목은 «어디에도 넣지 않는다» — 잘못 물려주는 것보다
        # 빠뜨리는 쪽이 안전하다(이 스크립트는 스크리닝이지 최종 판정이 아니다)
    if not 태그됨:
        return None  # 실제로 다중비목 나열이 아니다(번호는 있지만 다 같은 얘기)
    return {biz: "\n".join(chunks) for biz, chunks in 태그됨.items()}


def 근거텍스트(본문: str, 비목: str) -> str:
    """이 룰의 비목에 «해당하는 부분만» 돌려준다. 다중비목 문서가 아니면 전문 그대로."""
    섹션 = 비목별_섹션(본문)
    if 섹션 is not None:
        return 섹션.get(비목, "")  # 표 안에 이 비목이 없으면 빈 문자열(전문 오염 방지)
    번호섹션 = 번호목록_섹션(본문)
    if 번호섹션 is not None:
        return 번호섹션.get(비목, "")
    return 본문


def 문장_후보(본문: str) -> list[str]:
    """본문에서 «제약 후보 문장/불릿» 을 뽑는다.

    조문(프로즈)은 "다." 로, 붙임2(표)는 "•" 로 나뉜다 — 마침표 규칙이 다르다.
    둘 다 시도하고, 끝-어미가 걸리는 조각만 남긴다.
    """
    if not 본문:
        return []
    flat = 본문.replace("\n", " ")
    조각들: list[str] = []
    # 1) "다." 기준 (조문 프로즈)
    조각들 += [s.strip() for s in re.split(r"(?<=다\.)\s*", flat) if s.strip()]
    # 2) "•" 기준 (붙임2 해설표 불릿) — 🔴 줄(표 행) 단위로 먼저 자르고 그 안에서 "•" 로
    #    나눈다. 통째로 자르면 다음 행 라벨("| 기 타 |")이 꼬리에 붙어 어미 검사가 깨진다
    #    (실측: '창업 활동비' 유의사항 불릿이 이 버그로 안 걸렸었다)
    for 행 in 본문.split("\n"):
        if "•" not in 행:
            continue
        조각들 += [s.strip(" |") for s in 행.split("•") if s.strip(" |")]

    후보 = []
    seen = set()
    for s in 조각들:
        tail = s[-25:]
        if (금지_어미.search(tail) or 의무_어미.search(tail)) and s not in seen:
            # 표 셀 전체가 한 조각으로 잡히는 경우(파이프 다수)는 너무 길어 노이즈다 — 제외
            if s.count("|") > 3:
                continue
            후보.append(s)
            seen.add(s)
    return 후보


# ────────────────────────────────────────────────────────────────────
# [2단계] 느슨한 매칭 — 문자 바이그램 커버리지
# ────────────────────────────────────────────────────────────────────

_정규화_제거 = re.compile(r"[\s\|·•,\.\(\)『』「」<>\[\]\"'\-]+")


def _바이그램(s: str) -> set[str]:
    s = _정규화_제거.sub("", s)
    return {s[i : i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else set()


def 커버리지(문장: str, 룰텍스트블롭: str) -> float:
    """문장의 바이그램이 룰텍스트(금지예시+사전승인_조건 합본) 안에 얼마나 있는가.

    방향: 문장(«원문 근거») 기준 커버리지다 — 짧은 룰 발췌가 긴 원문 문장을
    «인용했는가» 를 보는 게 아니라, 원문 문장의 핵심이 룰 텍스트 «어딘가에» 박혀
    있는지를 본다. 룰 발췌가 짧아도(예: "퇴직급여충당금") 그 바이그램이 원문에
    다 들어있으면 자동으로 높게 나온다 — 반대 방향은 이 케이스에서 오히려 낮게 나옴.
    """
    문장_bg = _바이그램(문장)
    if not 문장_bg:
        return 0.0
    룰_bg = _바이그램(룰텍스트블롭)
    if not 룰_bg:
        return 0.0
    return len(문장_bg & 룰_bg) / len(문장_bg)


def 룰텍스트블롭(rule: dict) -> str:
    parts = []
    for x in rule.get("금지예시") or []:
        parts.append(str(x))
    for x in rule.get("사전승인_조건") or []:
        parts.append(str(x))
    return " ".join(parts)


# ────────────────────────────────────────────────────────────────────
# 본체
# ────────────────────────────────────────────────────────────────────


def 근거조문_로드(rules: list[dict], cur) -> tuple[dict, list[tuple]]:
    pairs = set()
    for r in rules:
        for g in r.get("근거") or []:
            doc_id, jo = g.get("doc_id"), g.get("조번호")
            if doc_id and jo:
                pairs.add((doc_id, jo))
    result: dict[tuple, list[dict]] = {}
    못찾음: list[tuple] = []
    for doc_id, jo in pairs:
        cur.execute(
            "SELECT 조제목, 본문, 삭제 FROM corpus.doc_articles WHERE doc_id=%s AND 조번호=%s",
            (doc_id, jo),
        )
        rr = cur.fetchall()
        살아있는 = [x for x in rr if not x[2]]
        if not 살아있는:
            못찾음.append((doc_id, jo))
        else:
            result[(doc_id, jo)] = [{"조제목": x[0], "본문": x[1]} for x in 살아있는]
    return result, 못찾음


def 검출력(rules_by_id: dict, articles: dict, 임계: float) -> dict:
    """P3-a 가 손으로 찾은 6건을 검산기가 재현하는지 — 이 스크립트의 관문."""
    손6건 = [431, 441, 427, 437, 464, 435]
    재현 = []
    상세 = {}
    for rid in 손6건:
        rule = rules_by_id[rid]
        블롭 = 룰텍스트블롭(rule)
        누락, _ = 행_평가(rule, articles, 임계)
        상세[rid] = [n["조문_금지문장"][:40] for n in 누락]
        if 누락:
            재현.append(rid)
    return {"손으로찾은6건": 손6건, "검산기재현": 재현,
            "재현율": f"{len(재현)}/{len(손6건)}", "상세": 상세}


def 행_평가(rule: dict, articles: dict, 임계: float) -> tuple[list[dict], int]:
    블롭 = 룰텍스트블롭(rule)
    비목 = rule.get("비목") or ""
    전체후보 = []
    for g in rule.get("근거") or []:
        doc_id, jo = g.get("doc_id"), g.get("조번호")
        for a in articles.get((doc_id, jo), []):
            스코프본문 = 근거텍스트(a["본문"] or "", 비목)
            for s in 문장_후보(스코프본문):
                전체후보.append((doc_id, jo, s))
    # 같은 문장이 여러 근거에서 중복되면 하나로
    seen = set()
    dedup = []
    for doc_id, jo, s in 전체후보:
        if s in seen:
            continue
        seen.add(s)
        dedup.append((doc_id, jo, s))

    누락 = []
    for doc_id, jo, s in dedup:
        cov = 커버리지(s, 블롭)
        if cov < 임계:
            누락.append({
                "근거": f"{doc_id}::{jo}",
                "조문_금지문장": s,
                "룰에_담긴것": 블롭 if 블롭 else "(없음)",
                "커버리지": round(cov, 3),
            })
    return 누락, len(dedup)


def main() -> None:
    rules = json.loads((SCRATCH / "_k_rules_full.json").read_text(encoding="utf-8"))
    rules_by_id = {r["rule_id"]: r for r in rules}
    conn = db.connect(); cur = conn.cursor()
    articles, 못찾음 = 근거조문_로드(rules, cur)

    # 임계 탐색: 손으로 찾은 6건(부재해야 함)과, 잘 담긴 걸로 확인된 대조 사례(존재해야 함)로 캘리브레이션
    대조_존재사례 = [
        (430, "인건비를 통한 근로자의 퇴직급여충당금은 집행불가"),        # 예비 인건비 — 붙임2 원문
        (426, "귀금속, 보석, 원석 등은 원칙적으로 구매할 수 없으나"),      # 재료비 귀금속
    ]
    print("=== 임계 캘리브레이션 ===")
    for cand_임계 in (0.15, 0.2, 0.25, 0.3, 0.35, 0.4):
        det = 검출력(rules_by_id, articles, cand_임계)
        대조_커버 = [round(커버리지(s, 룰텍스트블롭(rules_by_id[rid])), 3)
                    for rid, s in 대조_존재사례]
        print(f"임계={cand_임계}  재현율={det['재현율']}  대조존재사례_커버리지={대조_커버}")

    임계 = 0.25  # 아래 실행 로그로 확정 (재현 6/6 유지 + 대조 존재사례가 임계 위에 남는 값)
    det = 검출력(rules_by_id, articles, 임계)
    print("\n=== 채택 임계", 임계, "===")
    print(json.dumps(det, ensure_ascii=False, indent=1))

    # 전수 82행
    # 🔴 TIPS 8행(491~499, verified=false)은 비목명이 "연구재료비"·"학생인건비" 등으로
    #    다른 8사업(창업지원 표준 비목 10종)과 완전히 다르고, 근거 문서도 별개
    #    ("국가연구개발사업 연구개발비 사용 기준")다. 위 비목별_섹션/번호목록_섹션
    #    스코핑은 이 비목명을 모른다 — 스코핑 없이 큰 문서 전체가 후보로 잡혀
    #    행당 30~38건이 나온다(실측: rule 496 단독 38건). 이건 조건누락이 아니라
    #    «스코핑 미지원» 이다 — 숫자를 섞으면 82행 집계가 거짓말이 된다. 따로 뺀다.
    스코핑_미지원행 = []
    누락후보_전체 = []
    행별_집계 = []
    for r in rules:
        # 🔴 TIPS 는 사업명으로 명시 제외한다 — 비목 "인건비" 는 이름이 같아 목록엔
        #    걸리지만 근거 문서(국가연구개발사업 연구개발비 사용 기준)가 완전히 달라
        #    비목명 일치만으론 스코핑이 안 통한다(실측: rule 495 단독 32건 노이즈).
        if r.get("사업명") == "TIPS" or (r.get("비목") or "") not in 비목_목록:
            스코핑_미지원행.append({"rule_id": r["rule_id"], "사업명": r["사업명"], "비목": r["비목"],
                                "사유": "TIPS(다른 근거체계) 또는 비목 동의어 사전에 없음 — "
                                        "스코핑이 전문 그대로 통과해 신뢰 불가"})
            continue
        누락, 총후보수 = 행_평가(r, articles, 임계)
        행별_집계.append({
            "rule_id": r["rule_id"], "조문금지문장수": 총후보수,
            "룰이담은수": 총후보수 - len(누락), "누락": len(누락),
        })
        for n in 누락:
            근거계층 = "L1" if "L1_" in n["근거"] else "L2"
            누락후보_전체.append({
                "rule_id": r["rule_id"], "사업명": r["사업명"], "비목": r["비목"],
                "근거": n["근거"], "조문_금지문장": n["조문_금지문장"],
                "룰에_담긴것": n["룰에_담긴것"], "커버리지": n["커버리지"],
                "근거계층": 근거계층,
                "확신도": "높음" if n["커버리지"] < 0.1 else ("중간" if n["커버리지"] < 임계 else "낮음"),
            })

    출력 = {
        "레인": "K", "대상행수": len(rules),
        "실집계행수": len(rules) - len(스코핑_미지원행),
        "스코핑_미지원행": 스코핑_미지원행,
        "검출력": {
            "손으로찾은6건": det["손으로찾은6건"], "검산기재현": det["검산기재현"],
            "재현율": det["재현율"],
            "어미목록": {"금지형": 금지_어미.pattern, "의무형": 의무_어미.pattern},
            "임계": 임계,
            "임계근거": ("문자 바이그램 커버리지. 손으로 찾은 6건(부재 확인됨) 전부가 "
                     f"이 임계 미만이면서, 대조 존재사례 {대조_존재사례[0][1][:12]}...(430)/"
                     f"{대조_존재사례[1][1][:12]}...(426) 는 이 임계 이상으로 남는 값을 "
                     "0.15~0.4 스윕으로 찾아 0.25로 정함 (위 캘리브레이션 로그 참고)"),
        },
        "누락후보": 누락후보_전체,
        "행별_집계": 행별_집계,
        "근거원문_못읽은행": [{"doc_id": d, "조번호": j} for d, j in 못찾음],
    }
    (SCRATCH / "조건누락_전수_82.json").write_text(
        json.dumps(출력, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n저장 완료: 누락후보 {len(누락후보_전체)}건, 근거못읽음 {len(못찾음)}건")


if __name__ == "__main__":
    main()
