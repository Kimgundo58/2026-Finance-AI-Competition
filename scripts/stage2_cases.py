# -*- coding: utf-8 -*-
"""Stage 2-e : 사례집 -> `corpus.case_chunks` (**B등급 · 판단불가 경로 전용**).

`stage2_chunk.py` 와 짝이지만 **다른 테이블에 넣는다.** 판정 인덱스(`corpus.chunks`)와
사례 인덱스(`corpus.case_chunks`)의 분리는 프롬프트가 아니라 물리 분리로 강제한다는
것이 CLAUDE.md 의 확정 원칙이고, 이 파일은 그 뒤쪽 절반이다.

🔴 이 스크립트는 `corpus.chunks` 를 건드리지 않는다. `stage2_chunk.py` 와 달리
   TRUNCATE 대상은 `case_chunks` 뿐이다.

절차
────
    check    F1 오염검사. **적재보다 먼저 돌린다.** 정답셋 ↔ 사례집 대조
    extract  Q&A 추출 -> _stage2_cases.jsonl (+ 품질 리포트)
    embed    CPU KURE-v1 임베딩 -> _stage2_cases.npy   (🔴 GPU 를 열지 않는다)
    load     jsonl + npy -> corpus.case_chunks
    all      위 넷을 순서대로

왜 `check` 가 먼저인가 (F1)
──────────────────────────
정답셋 적대적 27문항은 **「창업사업화 지원사업 부정행위 방지 사례집」(중기부)의 실제
제재 사례를 무해해 보이는 사전 질문으로 변환해서** 만들었다 (`적대적세트_초안.json`
의 `목적` 필드). 사례집을 그대로 `case_chunks` 에 넣으면, 판단불가 경로가 사용자에게
정답을 그대로 보여준다. `case_chunks` 는 판정 인덱스가 아니라 index_guard 의 기존
경로 블랙리스트에 걸리지 않는다 — 그래서 **여기서 따로 잰다.**

실행
────
    PYTHONIOENCODING=utf-8 python scripts/stage2_cases.py check
    PYTHONIOENCODING=utf-8 python scripts/stage2_cases.py extract
    PYTHONIOENCODING=utf-8 python scripts/stage2_cases.py embed
    PYTHONIOENCODING=utf-8 python scripts/stage2_cases.py load
    PYTHONIOENCODING=utf-8 python scripts/stage2_cases.py all
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import index_guard                                                   # noqa: E402
import psycopg                                                       # noqa: E402

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")
WORK = ROOT / "scripts" / "_work"
DATA = ROOT / "2026_Finance_DATA_FOR_RAG"

OUT_JSONL = WORK / "_stage2_cases.jsonl"
OUT_NPY = WORK / "_stage2_cases.npy"
OUT_AUDIT = WORK / "_F_오염검사.json"

MODEL = "nlpai-lab/KURE-v1"
DIM = 1024
MAX_SEQ = 1024

ANSWER_CAP = 2000        # 답변 표시 상한. 임베딩은 question 만 하므로 검색에 영향 없다
MIN_Q = 10               # 이보다 짧은 질문은 목차 파편이다
MIN_A = 20


# ════════════════════════════════════════════════════════════════════════════
# 후보 목록 — 문서마다 "어떤 파서로 읽는가"와 "왜 넣는가/빼는가"를 한곳에 적는다.
#
# 🔴 `사유` 는 주석이 아니라 산출물이다. `check` 가 이걸 그대로 보고서에 옮긴다.
#    나중에 "왜 이 문서는 빠졌나" 를 코드가 아니라 보고서에서 읽을 수 있어야 한다.
# ════════════════════════════════════════════════════════════════════════════
후보 = [
    # ── 넣는다 ────────────────────────────────────────────────────────────
    {"doc_id": "사례집_경인교대_산학협력단연구비집행FAQ_목록",
     "파서": "qa_colon", "출처도메인": "R&D", "채택": True,
     "사유": "산학협력단 연구비집행 FAQ 77건. Q:/A: 구조가 원본 게시판 그대로라 파싱 손실 0"},
    {"doc_id": "사례집_한국연구재단_정부연구비사용QA사례집_2025개정",
     "파서": "qa_numbered", "출처도메인": "R&D", "채택": True,
     "사유": "Q<번호>. 제목 + 상세 + A. 구조. 2025 개정판이라 현행 혁신법 기준"},
    {"doc_id": "사례집_미래창조과학부_연구비사용상담부당집행사례집_2013",
     "파서": "qa_space", "출처도메인": "R&D", "채택": True,
     "사유": "상담사례 Q/A 33건. 2013년 자료라 근거 법령이 구판 — 답변 본문에 "
             "「국가연구개발사업의 관리 등에 관한 규정」(폐지) 인용이 섞인다. "
             "B등급 라벨과 '적용되지 않습니다' 고지로 감당한다"},
    {"doc_id": "판례_행정심판재결례_보조금환수_deccSeq243657",
     "파서": "재결", "출처도메인": "보조금", "채택": True,
     "사유": "중소기업기술개발 지원사업 출연금 환수 재결 1건. 사건명이 질문 자리를 채운다"},

    # ── 뺀다 ──────────────────────────────────────────────────────────────
    {"doc_id": "창업사업화 지원사업 부정행위 방지 사례집",
     "파서": None, "출처도메인": "창업", "채택": False,
     "사유": "🔴 F1 오염. 골든셋 적대적 27문항 전량이 이 문서의 사례를 변환한 것이다 "
             "(`적대적세트_초안.json` 의 `사례출처` 필드가 문항마다 쪽수까지 명시). "
             "판단불가 경로가 정답을 그대로 보여준다. check 가 수치로 재확인한다"},
    {"doc_id": "사례집_권익위재결례집_1",
     "파서": None, "출처도메인": "보조금", "채택": False,
     "사유": "일반 행정심판 재결례집. 79건 중 보조금·연구개발 키워드 적중 11건뿐이고 "
             "그 11건도 산업집적법 출연금·고용보험료라 창업지원금 지출과 무관하다. "
             "게다가 사건명이 목차에만 있고 【재결요지】 블록에 없어 question 을 "
             "신뢰성 있게 만들 수 없다 (목차↔본문 결합은 깨지기 쉬운 파싱)"},
    {"doc_id": "사례집_권익위재결례집_2",
     "파서": None, "출처도메인": "보조금", "채택": False,
     "사유": "위와 같다. 76건 중 키워드 적중 3건"},
    {"doc_id": "사례집_IITP_제재처분가이드라인_2025",
     "파서": None, "출처도메인": "R&D", "채택": False,
     "사유": "가이드라인 산문. Q&A·사례 단위 경계가 없다 (Q/A 마커 0). 사례 인덱스는 "
             "'질문↔질문' 검색이라 질문이 없는 문서는 넣을 자리가 없다"},
    {"doc_id": "사례집_KISTEP_국가연구개발사업제재처분판례조사분석_2022",
     "파서": None, "출처도메인": "R&D", "채택": False,
     "사유": "연구보고서. 판례 요약이 본문 산문에 섞여 있고 사건 단위 경계가 없다"},
    {"doc_id": "사례집_기획재정부_e나라도움보조사업자매뉴얼",
     "파서": None, "출처도메인": "보조금", "채택": False,
     "사유": "시스템 조작 매뉴얼(메뉴 경로 안내). 지출 판정 사례가 아니다"},
    {"doc_id": "사례집_기획재정부_국고보조금통합관리지침_2021-210호",
     "파서": None, "출처도메인": "보조금", "채택": False,
     "사유": "🔴 사례집이 아니라 **규정 원문**이다 (doc_type='지침'). 사례로 넣으면 "
             "'사례는 근거가 아니다' 라벨을 단 채 규정이 노출된다 — 라벨과 내용이 "
             "어긋나 사용자를 오도한다. 규정이 필요하면 판정 인덱스 쪽 문제다"},
]

채택_ids = {c["doc_id"] for c in 후보 if c["채택"]}


# ════════════════════════════════════════════════════════════════════════════
# 파서
# ════════════════════════════════════════════════════════════════════════════
# 🔴 태그 이름을 화이트리스트로 잡는다. `<[^>]*>` 로 뭉뚱그리면 규정 표기인
#    `<표-3>` · `<붙임1>` · `<주의 사항>` 이 통째로 날아간다 — 사례 답변에서 그
#    표기가 사라지면 무엇을 가리키는지 알 수 없게 된다.
#    (초판은 `<[^>]{1,40}>` 로 길이 제한을 뒀는데, 경인교대 게시판의
#     `<span style="color: rgb(118,118,118); font-family: …">` 가 110자라 그대로 샜다)
RE_HTML = re.compile(
    r"</?(?:span|div|p|br|hr|b|i|u|em|strong|font|a|img|ul|ol|li|"
    r"table|thead|tbody|tr|td|th|h[1-6]|blockquote|pre|code)\b[^>]*>",
    re.I)
RE_HTML엔티티 = re.compile(r"&(nbsp|lt|gt|amp|quot|#\d+);")
RE_페이지노이즈 = re.compile(r"(?m)^\s*(▪+\s*\d+|\d+\s*▪+|\d{1,3})\s*$")
RE_공백 = re.compile(r"[ \t]+")

_엔티티 = {"nbsp": " ", "lt": "<", "gt": ">", "amp": "&", "quot": '"'}


def _정리(s: str, cap: int | None = None, *, 한줄: bool = False) -> str:
    """PDF·게시판 산출물을 사람이 읽는 문장으로.

    `한줄=True` 는 질문용이다. PDF 는 폭에 맞춰 줄을 끊으므로 질문이
    "집행할 수\\n있나요?" 로 쪼개져 들어온다 — 그대로 임베딩하면 문장이 아니다.
    """
    s = RE_HTML.sub(" ", s or "")
    s = RE_HTML엔티티.sub(lambda m: _엔티티.get(m.group(1), " "), s)
    s = RE_페이지노이즈.sub("", s)
    if 한줄:
        s = re.sub(r"\s*\n\s*", " ", s)
    else:
        # 단일 개행은 PDF 줄바꿈이라 공백으로. 빈 줄(문단 경계)만 남긴다
        s = re.sub(r"(?<!\n)\n(?!\n)", " ", s)
    s = RE_공백.sub(" ", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    if cap and len(s) > cap:
        s = s[:cap].rstrip() + " …"
    return s


def p_qa_colon(t: str) -> list[dict]:
    """`[N] (page P)` / `Q: ...` / `A: ...` — 경인교대 FAQ 게시판."""
    out = []
    for b in re.split(r"(?m)^(?=\[\d+\] \(page)", t)[1:]:
        m = re.match(r"\[(\d+)\] \(page (\d+)\)\s*\nQ:\s*(.*?)\nA:\s*(.*)", b, re.S)
        if not m:
            continue
        out.append({"번호": m.group(1), "쪽": m.group(2),
                    "question": _정리(m.group(3), 한줄=True),
                    "answer": _정리(m.group(4), ANSWER_CAP)})
    return out


def p_qa_numbered(t: str) -> list[dict]:
    """`Q<n>. 제목` / 상세 / `A. 답변` — 한국연구재단 Q&A 사례집.

    제목만으로는 검색이 안 된다 ("‘인건비계상률’은 무엇?"). 그 아래 상세 문장이
    실제 사용자 질문이라 **제목 + 상세를 합쳐서** question 으로 쓴다.
    """
    out = []
    for b in re.split(r"(?m)^(?=Q\s?\d+\s?\.)", t)[1:]:
        m = re.match(r"Q\s?(\d+)\s?\.\s*(.*?)\n(.*?)^A\s?\.\s*(.*)", b, re.S | re.M)
        if not m:
            continue                      # 답이 표인 문항(Q7). 억지로 넣지 않는다
        제목 = _정리(m.group(2), 한줄=True)
        상세 = _정리(m.group(3), 한줄=True)
        q = f"{제목} {상세}".strip() if 상세 else 제목
        out.append({"번호": m.group(1), "쪽": None,
                    "question": q, "answer": _정리(m.group(4), ANSWER_CAP)})
    return out


def p_qa_space(t: str) -> list[dict]:
    """`Q <질문>` / `A <답변>` — 미래창조과학부 상담·부당집행 사례집."""
    out = []
    for i, b in enumerate(re.split(r"(?m)^(?=Q\s+\S)", t)[1:], 1):
        m = re.match(r"Q\s+(.*?)\n\s*A\s+(.*)", b, re.S)
        if not m:
            continue
        # 답변이 다음 장 제목까지 흘러간다. 장 경계에서 자른다.
        a = re.split(r"(?m)^\s*제\s?\d\s?장\s", m.group(2))[0]
        out.append({"번호": str(i), "쪽": None,
                    "question": _정리(m.group(1), 한줄=True),
                    "answer": _정리(a, ANSWER_CAP)})
    return out


def p_재결(t: str) -> list[dict]:
    """단건 행정심판 재결례. 첫 줄 사건명이 질문 자리, 【재결요지】가 답 자리."""
    사건명 = t.strip().split("\n", 1)[0].strip()
    m = re.search(r"【재결요지】(.*?)(?=【주문】|$)", t, re.S)
    if not m or len(사건명) < MIN_Q:
        return []
    return [{"번호": "1", "쪽": None,
             "question": _정리(사건명, 한줄=True),
             "answer": _정리(m.group(1), ANSWER_CAP)}]


파서표 = {"qa_colon": p_qa_colon, "qa_numbered": p_qa_numbered,
          "qa_space": p_qa_space, "재결": p_재결}


def 문서본문(cur, doc_id: str) -> str:
    cur.execute("SELECT string_agg(본문, chr(10) ORDER BY article_id) "
                "FROM corpus.doc_articles WHERE doc_id = %s", (doc_id,))
    r = cur.fetchone()
    return (r[0] if r else None) or ""


def 추출(cur, spec: dict) -> list[dict]:
    쌍 = 파서표[spec["파서"]](문서본문(cur, spec["doc_id"]))
    쌍 = [p for p in 쌍 if len(p["question"]) >= MIN_Q and len(p["answer"]) >= MIN_A]
    for p in 쌍:
        p["doc_id"] = spec["doc_id"]
        p["출처도메인"] = spec["출처도메인"]
    return 쌍


# ════════════════════════════════════════════════════════════════════════════
# F1 — 오염 검사
# ════════════════════════════════════════════════════════════════════════════
NGRAM = 6
겹침_임계 = 0.35          # 6-gram containment. 이 위면 "같은 문장을 쓰고 있다"
유사_임계 = 0.80          # KURE-v1 코사인. 이 위면 "같은 사례를 묻고 있다"
표본상한 = 120            # Q&A 구조 없는 문서의 의미 측정 표본 수 (아래 _후보단위 주석)

# 🔴 정답셋 `사례출처` 문자열 -> 후보 doc_id.
#    이게 가장 강한 신호다. 아래 두 수치 검사보다 **먼저** 본다 —
#    실측(2026-08-31)에서 어휘 겹침은 0.248 로 임계 미만이었다. 적대적 문항이
#    사례를 **다시 쓴** 것이라 표현이 안 겹친다. 수치만 믿었으면 통과시켰을 것이다.
출처_문서맵 = {
    "부정행위 사례집": "창업사업화 지원사업 부정행위 방지 사례집",
}

RE_비문자 = re.compile(r"[^0-9A-Za-z가-힣]+")


def _grams(s: str, n: int = NGRAM) -> set[str]:
    s = RE_비문자.sub("", s or "")
    return {s[i:i + n] for i in range(len(s) - n + 1)} if len(s) >= n else set()


def _containment(a: set[str], b: set[str]) -> float:
    """작은 쪽 기준 포함률. Jaccard 는 길이 차가 크면 0 으로 눌려 못 쓴다."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _골든문항() -> list[dict]:
    """정답셋 77문항 + 적대적 초안(사례출처 필드가 여기에만 있다)."""
    확정 = json.loads((DATA / "_골든셋_스테이징" / "_골든셋_확정본.json")
                      .read_text(encoding="utf-8"))["문항"]
    적대 = json.loads((DATA / "_골든셋_스테이징" / "적대적세트_초안.json")
                      .read_text(encoding="utf-8"))["문항"]
    출처맵 = {i["no"]: i.get("사례출처") for i in 적대}
    out = []
    for g in 확정:
        본문 = " ".join(filter(None, [
            g.get("질문") or "", g.get("근거_원문") or "",
            " ".join(g.get("해야할일") or []),
        ]))
        out.append({"no": g.get("no"), "세트": g.get("_세트"), "질문": g.get("질문"),
                    "본문": 본문, "사례출처": 출처맵.get(g.get("no"))})
    return out


def _후보단위(cur, spec: dict) -> tuple[list[str], list[str], str | None]:
    """(검색노출문자열, 어휘대조문자열, 비고). 오염은 두 축을 따로 잰다.

    - 검색노출 = `question` 만. 실제로 `유사사례()` 가 매칭하는 것이 이것이다
    - 어휘대조 = question + answer. 답이 통째로 새는지를 본다
    """
    if spec["파서"]:
        쌍 = 추출(cur, spec)
        return ([p["question"] for p in 쌍],
                [f'{p["question"]} {p["answer"]}' for p in 쌍], None)

    # 부정행위 사례집은 doc_articles 가 0행이다 (스캔 PDF). OCR json 으로 잰다
    if spec["doc_id"] == "창업사업화 지원사업 부정행위 방지 사례집":
        ocr = json.loads((DATA / "사례집" / "_부정행위사례집_OCR.json")
                         .read_text(encoding="utf-8"))
        노출 = [c["title"] for c in ocr["cases"]]
        전체 = [f'{c["title"]} {" ".join(c["body"])} {" ".join(c.get("grounds") or [])}'
                for c in ocr["cases"]]
        return 노출, 전체, "doc_articles 0행(스캔 PDF) — _부정행위사례집_OCR.json 으로 측정"

    # Q&A 구조가 없는 문서. 애초에 사례 인덱스에 들어갈 단위가 없다 —
    # 노출될 것이 없으므로 의미 측정이 채택 결정을 바꾸지 못한다. 그래서 **표본만**
    # 잰다. (초판은 전량을 쟀는데 권익위 두 권 1,116조각을 CPU 로 도느라 40분을
    # 넘겼다. 결정에 쓰이지 않는 수치에 그 시간을 쓸 이유가 없다.)
    t = 문서본문(cur, spec["doc_id"])
    조각 = [t[i:i + 1500] for i in range(0, len(t), 1500)] if t else []
    if len(조각) <= 표본상한:
        return 조각, 조각, "Q&A 구조 없음 — 1,500자 조각으로 근사 측정"
    걸음 = len(조각) / 표본상한
    표본 = [조각[int(i * 걸음)] for i in range(표본상한)]
    return (표본, 조각,
            f"Q&A 구조 없음 · 조각 {len(조각)}개 중 {표본상한}개 등간격 표본으로 "
            f"의미 측정 (어휘는 전량). 사례 단위가 없어 노출될 것이 없으므로 "
            f"이 수치는 채택 결정에 쓰이지 않는다")


def cmd_check(a) -> None:
    골든 = _골든문항()
    골든g = [_grams(g["본문"]) for g in 골든]

    # ── ① 출처 추적 : 정답셋이 스스로 밝힌 원 사례 ────────────────────────
    출처집계: dict[str, list[str]] = {}
    for g in 골든:
        if g["사례출처"]:
            키 = g["사례출처"].split("[")[0].strip() or g["사례출처"]
            출처집계.setdefault(키, []).append(g["no"])
    출처_오염문서 = {출처_문서맵[k]: v for k, v in 출처집계.items() if k in 출처_문서맵}
    미매핑출처 = [k for k in 출처집계 if k not in 출처_문서맵]

    print("① 출처 추적 — 골든셋이 스스로 밝힌 원 사례")
    for k, v in sorted(출처집계.items(), key=lambda x: -len(x[1])):
        tag = f'-> {출처_문서맵[k]}' if k in 출처_문서맵 else "-> ⚠️ 문서 미매핑"
        print(f"   {k}  ← {len(v)}문항  {tag}")
    if not 출처집계:
        print("   (없음)")

    # ── ②③ 어휘 · 의미 대조 ───────────────────────────────────────────────
    print("\n② 어휘 대조 준비 · ③ 의미 대조용 CPU 임베딩 …", flush=True)
    from sentence_transformers import SentenceTransformer
    import numpy as np
    m = SentenceTransformer(MODEL, device="cpu")
    m.max_seq_length = MAX_SEQ
    gv = m.encode([g["질문"] for g in 골든], batch_size=16,
                  normalize_embeddings=True, convert_to_numpy=True)

    보고 = []
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        for spec in 후보:
            노출, 전체, 비고 = _후보단위(cur, spec)
            row = {"doc_id": spec["doc_id"], "채택_사전": spec["채택"],
                   "사유": spec["사유"], "파서": spec["파서"], "단위수": len(전체)}
            if 비고:
                row["비고"] = 비고

            # ② 어휘
            단위g = [_grams(u) for u in 전체]
            어휘 = [max((_containment(gg, ug) for ug in 단위g), default=0.0) for gg in 골든g]

            # ③ 의미 — 실제 유사사례() 가 하는 것과 같은 계산 (질문 ↔ question)
            if 노출:
                cv = m.encode(노출, batch_size=16, normalize_embeddings=True,
                              convert_to_numpy=True)
                의미 = (gv @ cv.T).max(axis=1)
            else:
                의미 = np.zeros(len(골든))

            적중 = [{"no": g["no"], "세트": g["세트"], "질문": g["질문"][:60],
                     "어휘": round(float(a_), 3), "의미": round(float(b_), 3)}
                    for g, a_, b_ in zip(골든, 어휘, 의미)
                    if a_ >= 겹침_임계 or b_ >= 유사_임계]
            row["최대어휘"] = round(float(max(어휘, default=0.0)), 3)
            row["최대의미"] = round(float(max(의미, default=0.0)), 3)
            row["임계초과문항"] = 적중
            row["오염신호"] = [s for s, on in (
                ("출처", spec["doc_id"] in 출처_오염문서),
                ("어휘", row["최대어휘"] >= 겹침_임계),
                ("의미", row["최대의미"] >= 유사_임계)) if on]
            row["오염"] = bool(row["오염신호"])
            보고.append(row)

    print(f"\n   임계 — 어휘 6-gram containment ≥ {겹침_임계} · 의미 KURE 코사인 ≥ {유사_임계}")
    print(f'   {"문서":52s} {"단위":>5s} {"어휘":>6s} {"의미":>6s}  판정')
    for r in 보고:
        판정 = ("🔴 오염(" + "+".join(r["오염신호"]) + ")") if r["오염"] else \
               ("채택" if r["채택_사전"] else "제외(품질)")
        print(f'   {r["doc_id"][:50]:52s} {r["단위수"]:5d} {r["최대어휘"]:6.3f} '
              f'{r["최대의미"]:6.3f}  {판정}')

    # ── ④ 최종 채택 = 사전 채택 ∧ 오염 아님 ───────────────────────────────
    최종 = [r["doc_id"] for r in 보고 if r["채택_사전"] and not r["오염"]]
    뒤집힘 = [r["doc_id"] for r in 보고 if r["채택_사전"] and r["오염"]]

    OUT_AUDIT.parent.mkdir(parents=True, exist_ok=True)
    OUT_AUDIT.write_text(json.dumps({
        "생성": "F세션 2026-08-31 · scripts/stage2_cases.py check",
        "목적": "F1 — 적대적 골든셋 27문항이 부정행위 사례집 변환본이라 판단불가 "
                "경로가 정답을 그대로 보여줄 수 있다. 오염 문서를 가려 적재에서 뺀다",
        "방법": {
            "① 출처": "적대적세트_초안.json 의 `사례출처` 필드 -> 후보 doc_id 매핑",
            "② 어휘": f"{NGRAM}-gram containment(작은 쪽 기준) ≥ {겹침_임계}. "
                       "골든셋측 = 질문+근거_원문+해야할일 · 사례측 = question+answer",
            "③ 의미": f"KURE-v1 코사인 ≥ {유사_임계}. 골든셋 질문 ↔ 사례 question. "
                       "유사사례() 가 실제로 하는 계산과 같다",
            "🔴 셋을 다 재는 이유":
                "어휘만으로는 못 잡는다. 부정행위 사례집의 최대 어휘 겹침은 0.248 로 "
                "임계 미만이었다 — 적대적 문항이 사례를 '무해해 보이는 사전 질문'으로 "
                "다시 쓴 것이라 표현이 안 겹친다. 오염은 표현이 아니라 출처에 있다.",
        },
        "골든셋_출처집계": 출처집계,
        "출처_미매핑": 미매핑출처,
        "문서별": 보고,
        "최종채택": 최종,
        "오염으로_제외": 뒤집힘,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n④ 최종 채택 {len(최종)}건 · 오염 제외 "
          f'{len([r for r in 보고 if r["오염"]])}건')
    print(f"   보고서 {OUT_AUDIT.relative_to(ROOT)}")
    if 뒤집힘:
        print("   🔴 사전 채택이 오염으로 뒤집힌 문서:", ", ".join(뒤집힘))


# ════════════════════════════════════════════════════════════════════════════
# F2 — 추출
# ════════════════════════════════════════════════════════════════════════════
def _오염통과(doc_id: str) -> bool:
    """check 가 남긴 판정을 읽는다. 없으면 실행을 거부한다 — F1 이 먼저다."""
    if not OUT_AUDIT.exists():
        sys.exit("🔴 오염검사(_F_오염검사.json)가 없다. `stage2_cases.py check` 를 먼저 돌린다.\n"
                 "   F1 은 적재보다 먼저다 — 적대적 골든셋이 사례집 변환본이다.")
    a = json.loads(OUT_AUDIT.read_text(encoding="utf-8"))
    return doc_id in a["최종채택"]


def cmd_extract(a) -> None:
    rows: list[dict] = []
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        for spec in 후보:
            if not spec["채택"]:
                continue
            if not _오염통과(spec["doc_id"]):
                print(f'   ✗ {spec["doc_id"][:52]:54s} 오염검사에서 제외됨')
                continue
            # 🔴 판정 인덱스가 아니어도 통과 조건은 태운다 (계약 §2-5).
            cur.execute("SELECT src_path, layer, index_target FROM corpus.documents "
                        "WHERE doc_id = %s", (spec["doc_id"],))
            got = cur.fetchone()
            if not got:
                print(f'   ✗ {spec["doc_id"][:52]:54s} documents 에 없다')
                continue
            src, layer, tgt = got
            index_guard.assert_indexable(src, layer)
            if tgt:
                sys.exit(f'🔴 {spec["doc_id"]} 가 index_target=True 다. 사례 문서가 '
                         f'판정 인덱스 대상으로 잡혀 있다 — 적재 전에 원인을 밝힐 것.')
            쌍 = 추출(cur, spec)
            print(f'   ✓ {spec["doc_id"][:52]:54s} {len(쌍):4d}건  ({spec["파서"]})')
            rows.extend(쌍)

    OUT_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with OUT_JSONL.open("w", encoding="utf-8") as f:
        for i, r in enumerate(rows):
            r["seq"] = i                     # 🔴 jsonl 줄 순서 = npy 행 순서 계약
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    q = [len(r["question"]) for r in rows] or [0]
    print(f"\n추출 {len(rows)}건 -> {OUT_JSONL.relative_to(ROOT)}")
    print(f"   질문 길이 min {min(q)} / 중앙 {sorted(q)[len(q) // 2]} / max {max(q)}")


# ════════════════════════════════════════════════════════════════════════════
# F3 — 임베딩 (🔴 CPU. GPU 팟을 열지 않는다 — 계약 §5)
# ════════════════════════════════════════════════════════════════════════════
def cmd_embed(a) -> None:
    import time

    import numpy as np
    from sentence_transformers import SentenceTransformer

    rows = [json.loads(l) for l in OUT_JSONL.open(encoding="utf-8")]
    texts = [r["question"] for r in rows]          # 🔴 question 만. answer 아님
    print(f"입력 {len(texts):,}건 · CPU KURE-v1", flush=True)

    m = SentenceTransformer(MODEL, device="cpu")
    m.max_seq_length = MAX_SEQ
    t = time.time()
    v = m.encode(texts, batch_size=16, normalize_embeddings=True,
                 show_progress_bar=True, convert_to_numpy=True)
    assert v.shape == (len(texts), DIM), f"모양 불일치 {v.shape}"
    np.save(OUT_NPY, v.astype("float16"))
    print(f"임베딩 {time.time() - t:.0f}초 -> {OUT_NPY.relative_to(ROOT)}  {v.shape}")


# ════════════════════════════════════════════════════════════════════════════
# F2 — 적재
# ════════════════════════════════════════════════════════════════════════════
def cmd_load(a) -> None:
    import numpy as np

    rows = [json.loads(l) for l in OUT_JSONL.open(encoding="utf-8")]
    vecs = np.load(OUT_NPY).astype("float32")
    if vecs.shape != (len(rows), DIM):
        sys.exit(f"🔴 모양 불일치 — npy {vecs.shape} vs jsonl {len(rows)}행. "
                 "같은 jsonl 로 만든 npy 가 맞는지 확인할 것.")
    norms = np.linalg.norm(vecs, axis=1)
    if abs(norms.mean() - 1.0) > 0.01:
        vecs = vecs / np.clip(norms, 1e-9, None)[:, None]

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            # 통과 조건을 적재 직전에 한 번 더. extract 와 load 가 따로 돌 수 있다
            for doc_id in {r["doc_id"] for r in rows}:
                if not _오염통과(doc_id):
                    sys.exit(f"🔴 {doc_id} 는 오염검사 통과 목록에 없다. 적재 거부.")
                cur.execute("SELECT src_path, layer FROM corpus.documents WHERE doc_id=%s",
                            (doc_id,))
                index_guard.assert_indexable(*cur.fetchone())

            # 🔴 case_chunks 만 비운다. corpus.chunks 는 건드리지 않는다
            cur.execute("TRUNCATE corpus.case_chunks RESTART IDENTITY")
            with cur.copy("COPY corpus.case_chunks (doc_id, 출처도메인, question, "
                          "answer, embedding) FROM STDIN") as cp:
                for r, v in zip(rows, vecs):
                    cp.write_row((r["doc_id"], r["출처도메인"], r["question"],
                                  r["answer"],
                                  "[" + ",".join(f"{x:.6f}" for x in v) + "]"))
        conn.commit()
        n = conn.execute("SELECT count(*) FROM corpus.case_chunks").fetchone()[0]
        빈칸 = conn.execute("SELECT count(*) FROM corpus.case_chunks "
                            "WHERE embedding IS NULL").fetchone()[0]
        # 누수 확인 — chunks 는 손대지 않았어야 한다
        c = conn.execute("SELECT count(*) FROM corpus.chunks").fetchone()[0]
    print(f"적재 case_chunks {n:,}건 · embedding NULL {빈칸}건")
    print(f"   corpus.chunks {c:,}건 (건드리지 않았다)")


def cmd_all(a) -> None:
    cmd_check(a); print(); cmd_extract(a); print()
    cmd_embed(a); print(); cmd_load(a)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in [("check", cmd_check), ("extract", cmd_extract),
                     ("embed", cmd_embed), ("load", cmd_load), ("all", cmd_all)]:
        sub.add_parser(name).set_defaults(fn=fn)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
