# -*- coding: utf-8 -*-
"""Stage 2-c : 청킹 -> `corpus.chunks` 텍스트 확정. **임베딩은 하지 않는다.**

`build_index.py`(구 코퍼스 기준, 파이프라인 문서 §7-1 "재작성 대상")를 대체한다.
바뀐 것:

    분할 임계   3,000자          -> KURE 토크나이저 900토큰   (§3-4)
    첨부        비목표만 특수처리 -> **박스표만** 제외 (표는 룰의 재료). 산문 부속은 청킹한다
    삭제조      포함             -> 제외 (doc_articles 에는 남는다)
    범위 컷     없음             -> scripts/scope.py 범위밖_조()
    적용대상    없음             -> tag_apply_target.적용대상_of() 필수 경유
    임베딩      인라인 CPU 40분   -> 분리 (GPU. stage2_embed.py)

청킹과 임베딩을 나눈 이유: 청킹은 몇 초인데 임베딩은 로컬 CPU 로 4시간이다.
같은 스크립트에 묶여 있으면 청킹 규칙을 한 줄 고칠 때마다 4시간을 다시 쓴다.

실행:
    PYTHONIOENCODING=utf-8 python scripts/stage2_chunk.py --dry-run   # 세보기만
    PYTHONIOENCODING=utf-8 python scripts/stage2_chunk.py             # 적재
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# 🔴 import 순서가 중요하다. tag_apply_target 이 module-level 에서 sys.stdout 을
#    utf-8 로 다시 감싼다. 여기서 먼저 감싸면 그 래퍼가 닫혀 ValueError 가 난다.
#    그래서 **먼저 import 하고, 여기서는 다시 감싸지 않는다.**
import tag_apply_target as TAT                                       # noqa: E402
import index_guard                                                   # noqa: E402
from scope import 범위밖_조                                          # noqa: E402
from _lib import db                                                  # noqa: E402

DSN = db.DSN
APPLY = ROOT / "2026_Finance_DATA_FOR_RAG" / "_apply_target.json"
OUT_JSONL = ROOT / "scripts" / "_work" / "_stage2_chunks.jsonl"

MODEL = "nlpai-lab/KURE-v1"
MAX_TOK = 900          # 분할 임계. 헤더 ~50토큰 마진 포함해 1,024 아래를 지킨다 (§3-4)
GATE_TOK = 1024        # 게이트. 헤더+본문이 이걸 넘으면 꼬리가 임베딩에서 잘린다
MIN_CHARS = 50         # 이 아래는 직전 조각에 병합 — **같은 조 안에서만**

# ── 제외 대상 ────────────────────────────────────────────────────────────────
# 표는 청크가 아니라 룰의 재료다 (CLAUDE.md "별표·한도표는 RAG 에 넣지 않는다").
# ASCII 박스표(┌─┬─┐)를 그대로 임베딩하면 벡터가 표 서식에 끌려간다.
#   참고N  = 세부관리기준의 비목 정의·증빙 표. 이름만 다르고 별표와 같은 물건이다
#   부록   = 출입국관리법 서식집 48KB 같은 것
RE_첨부 = re.compile(r"^\s*(별표|별지|붙임|서식|첨부|별첨|부록|참고)")

# 🔴 2026-08-31 수정 — **이름이 아니라 내용으로 자른다.**
#
#    초판은 RE_첨부 이름만 보고 전량 제외했다. 그런데 실측하니 과잉 제외였다:
#      진입점 문서의 미청킹 부속 196조 중  박스표 146조 / **산문 50조(17만자)**
#    「붙임2 창업기업등 사업비 비목 해설표」(4,398자)는 ┌─┬─┐ 가 한 글자도 없는
#    산문 해설이다. "착수금 전액 선지급 가능한가" 같은 질문의 **유일한 근거**가 여기 있다.
#
#    실제 피해: 골든셋 적대적 세트 9문항의 정답 근거가 통째로 인덱스 밖이었다
#    (gold_id 51·57·58·59·60·62·73·75·76). 검색이 못 찾은 게 아니라 찾을 게 없었다.
#
#    원 취지("ASCII 박스표를 임베딩하면 벡터가 표 서식에 끌려간다")는 그대로 지킨다 —
#    다만 판정 기준을 **박스 괘선 문자의 실재**로 바꾼다. 표는 여전히 룰의 재료이지
#    청크가 아니다 (CLAUDE.md "별표·한도표는 RAG 에 넣지 않는다").
RE_박스표 = re.compile(r"[┌│└├┬┼┐┘┴┤─]")

# 괘선이 몇 개 이상이면 표로 본다. 산문에도 대시(─)가 장식으로 한두 개 섞일 수 있다.
박스표_임계 = 10


def 표인가(본문: str) -> bool:
    """부속 조가 ASCII 박스표인가 산문인가. 청킹 여부를 이걸로 가른다."""
    return len(RE_박스표.findall(본문 or "")) >= 박스표_임계

# ⚠️ TIPS 운영지침은 조가 아니라 `1.` `2.` … 35개 항목 구조다 (개요형).
#    본칙이므로 절대 제외하면 안 된다 — RE_첨부 가 이걸 잡지 않는 것을 확인했다.

RE_항 = re.compile(r"(?=[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳])")
RE_호 = re.compile(r"(?=\n\s*\d+\.\s)")
RE_목 = re.compile(r"(?=\n\s*[가-힣]\.\s)")
RE_장 = re.compile(r"제\s*(\d+)\s*장\s*([^\n<>]{0,20})")

# ── 사업명 ───────────────────────────────────────────────────────────────────
# 조회 키는 `corpus.precedence_rules.사업명` 이다. 다른 이름을 쓰면 조인이 조용히 빈다.
# (`build_citations.py` 의 SECTION_BIZ 는 "초격차 스타트업 1000+" 같은 다른 표기를
#  쓴다 — 그건 링크모음 문서의 절 제목이지 조회 키가 아니다.)
사업_of_doc: dict[str, list[str]] = {
    "예비창업패키지 세부관리기준(2025년)": ["예비창업패키지"],
    "초기창업패키지 세부관리기준(2025년)": ["초기창업패키지"],
    "2026년 재도전성공패키지 세부관리기준(11차 개정)": ["재도전성공패키지"],
    "창업도약패키지 세부관리기준(2025년)": ["창업도약패키지"],
    "창업중심대학 세부관리기준2025년 개정": ["창업중심대학"],
    "초격차 스타트업 프로젝트 세부관리기준(제10차)": ["초격차 스타트업 프로젝트"],
    "모두의 창업 프로젝트 세부관리기준(개정본)": ["모두의 창업 프로젝트"],
    "붙임1. 2026년 팁스TIPS 총괄 운영지침 3차 개정안 본문": ["TIPS"],
}

# 🔴 L1 은 사업명 NULL 이다 (= 전 사업 공통).
#   구 build_index 는 `_law_citations.json` 의 인용 관계로 L1 조문에 사업명을 박았다.
#   그건 "어느 사업 문서가 이 조를 인용했는가" 이지 "이 조가 그 사업에만 적용된다"가
#   아니다. 링크모음의 수집 범위가 곧 필터가 되어, 인용이 안 잡힌 사업에서 근거가
#   조용히 빠진다. 적용대상 기본값을 '공통' 으로 둔 것과 같은 판단이다 —
#   조용한 누락이 가장 나쁜 실패다. (통합관리지침은 8개 사업 전부에 적용된다)


# ── 청킹 ─────────────────────────────────────────────────────────────────────
def 토큰수(tok, texts: list[str]) -> list[int]:
    if not texts:
        return []
    enc = tok(texts, add_special_tokens=True, truncation=False,
              padding=False, return_attention_mask=False)
    return [len(x) for x in enc["input_ids"]]


def 분할(tok, 본문: str) -> list[tuple[str | None, str]]:
    """[(항호라벨, 텍스트)]. 900토큰 초과 -> 항 -> 호 -> 목 -> 토큰 강제 -> **재포장**.

    🔴 재포장이 핵심이다. 항 단위로 쪼개기만 하면 13,000자짜리 TIPS `4.` 가 항 59개
    = 청크 59개가 된다(실측). 조각 하나가 200자짜리라 문맥이 끊기고 인덱스만 부푼다.
    조 경계가 의미 경계라는 원칙(§3-4 오버랩 없음)의 반대편 — 조 **안**에서는
    900토큰 예산을 채울 때까지 연속한 항을 도로 붙인다. 자르는 위치는 항 경계 그대로다.
    """
    본문 = 본문.strip()
    if not 본문:
        return []
    if 토큰수(tok, [본문])[0] <= MAX_TOK:
        return [(None, 본문)]

    def 쪼개기(pat, s: str) -> list[str]:
        parts = [p.strip() for p in pat.split(s) if p and p.strip()]
        return parts if len(parts) > 1 else []

    잎: list[tuple[str | None, str]] = []           # 전부 MAX_TOK 이하
    항들 = 쪼개기(RE_항, 본문) or [본문]
    for i, (항, n) in enumerate(zip(항들, 토큰수(tok, 항들))):
        라벨 = 항[0] if 항[:1] in "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳" else (
            f"항{i+1}" if len(항들) > 1 else None)
        if n <= MAX_TOK:
            잎.append((라벨, 항))
            continue
        조각들 = 쪼개기(RE_호, 항) or 쪼개기(RE_목, 항) or [항]
        for j, (조각, m) in enumerate(zip(조각들, 토큰수(tok, 조각들))):
            꼬리 = f"{라벨 or ''}-{j+1}" if len(조각들) > 1 else 라벨
            if m <= MAX_TOK:
                잎.append((꼬리, 조각))
            else:
                # 구조로 더 못 쪼갠다. 줄 단위로 토큰 예산을 채워 자른다.
                for k, 토막 in enumerate(강제분할(tok, 조각)):
                    잎.append((f"{꼬리 or ''}#{k+1}", 토막))
    return 재포장(tok, 잎) or [(None, 본문)]


def 재포장(tok, 잎: list[tuple[str | None, str]]) -> list[tuple[str | None, str]]:
    """연속한 조각을 MAX_TOK 예산까지 도로 붙인다. 라벨은 `①~③` 로 범위 표기."""
    if len(잎) <= 1:
        return 잎
    길이 = 토큰수(tok, [t for _, t in 잎])
    묶음: list[list] = []                            # [[라벨들], [텍스트들], 토큰합]
    for (라벨, txt), n in zip(잎, 길이):
        if 묶음 and 묶음[-1][2] + n <= MAX_TOK:
            묶음[-1][0].append(라벨); 묶음[-1][1].append(txt); 묶음[-1][2] += n
        else:
            묶음.append([[라벨], [txt], n])
    out = []
    for 라벨들, 텍스트들, _ in 묶음:
        유효 = [l for l in 라벨들 if l]
        라벨 = None if not 유효 else (유효[0] if len(유효) == 1 else f"{유효[0]}~{유효[-1]}")
        out.append((라벨, "\n".join(텍스트들)))
    return out


def 강제분할(tok, s: str) -> list[str]:
    """구조가 없는 덩어리(표 잔재·긴 나열)를 줄 단위로 MAX_TOK 이하로 자른다."""
    줄들 = [l for l in s.split("\n")]
    길이 = 토큰수(tok, 줄들) if 줄들 else []
    out, buf, n = [], [], 0
    for 줄, ln in zip(줄들, 길이):
        if ln > MAX_TOK:                       # 한 줄이 이미 초과 — 문자로 자른다
            if buf:
                out.append("\n".join(buf)); buf, n = [], 0
            폭 = max(200, len(줄) * MAX_TOK // max(ln, 1) - 100)
            out += [줄[i:i + 폭] for i in range(0, len(줄), 폭)]
            continue
        if n + ln > MAX_TOK and buf:
            out.append("\n".join(buf)); buf, n = [], 0
        buf.append(줄); n += ln
    if buf:
        out.append("\n".join(buf))
    return [x for x in (t.strip() for t in out) if x]


def 병합(조각들: list[tuple[str | None, str]]) -> list[tuple[str | None, str]]:
    """50자 미만은 직전에 붙인다. **조 경계는 넘지 않는다** — 인용 단위가 깨진다."""
    out: list[list] = []
    for 라벨, txt in 조각들:
        if out and len(txt) < MIN_CHARS:
            out[-1][1] = out[-1][1] + "\n" + txt
        else:
            out.append([라벨, txt])
    return [(a, b) for a, b in out]


def 장맵(articles: list[dict]) -> dict[str, str]:
    """조번호 -> 그 조가 속한 장. 헤딩은 앞 조 본문 꼬리에 붙어 온다 (절_상속 과 같은 원리)."""
    out, state = {}, ""
    for a in articles:
        out[a["조번호"]] = state
        for m in RE_장.finditer(a.get("본문") or ""):
            제목 = re.sub(r"\s+", " ", m.group(2)).strip(" <>〈〉")
            state = f"제{m.group(1)}장 {제목}".strip()
    return out


def 헤더(layer, 사업, doc_id, 장, 조번호, 조제목) -> str:
    """임베딩·BM25 입력 전용 컨텍스트 헤더 (§3-5). chunks.text 는 건드리지 않는다."""
    문서 = re.sub(r"^(붙임\d*\.\s*|첨부\s*)", "", doc_id).strip()
    조 = f"{조번호}({조제목})" if 조제목 else str(조번호)
    칸 = [layer, "·".join(사업) if 사업 else "공통", 문서[:60], 장 or "-", 조]
    return "[" + " | ".join(칸) + "]"


# ── main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="세보기만. DB 를 쓰지 않는다")
    a = ap.parse_args()

    t0 = time.time()

    캐시 = Path.home() / ".cache/huggingface/hub/models--nlpai-lab--KURE-v1"
    if 캐시.exists():
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
    from transformers import AutoTokenizer
    print(f"토크나이저 로딩 {MODEL} ...", flush=True)
    tok = AutoTokenizer.from_pretrained(MODEL)

    태그 = TAT.태그맵(json.loads(APPLY.read_text(encoding="utf-8"))["tags"])
    print(f"  적용대상 태그 {len(태그)}건\n", flush=True)

    통계 = dict(문서=0, 조=0, 첨부컷=0, 삭제컷=0, 범위밖컷=0, 미결컷=0, 청크=0, 분할조=0)
    rows: list[tuple] = []
    임베딩입력: list[str] = []

    with db.connect() as conn:
        docs = conn.execute("""
            SELECT doc_id, layer, 기관ID, parse_quality, version, status,
                   retrieval_scope, src_path
              FROM corpus.documents
             WHERE index_target = TRUE AND layer IN ('L1','L2')
             ORDER BY layer, doc_id
        """).fetchall()

        for doc_id, layer, 기관, pq, ver, status, scope, src in docs:
            # 🔴 새 인덱싱 경로는 반드시 이 게이트를 태운다 (CLAUDE.md)
            index_guard.assert_indexable(src or doc_id, layer)
            통계["문서"] += 1

            arts = [dict(zip(("article_id", "조번호", "조제목", "본문", "페이지", "삭제"), r))
                    for r in conn.execute("""
                        SELECT article_id, 조번호, 조제목, 본문, 페이지, 삭제
                          FROM corpus.doc_articles WHERE doc_id = %s ORDER BY article_id
                    """, (doc_id,)).fetchall()]

            범위밖 = 범위밖_조(doc_id, arts)
            통계["범위밖컷"] += len(범위밖)
            장 = 장맵(arts)
            사업 = 사업_of_doc.get(doc_id)          # L1 은 None = 전 사업 공통

            for art in arts:
                통계["조"] += 1
                조번호 = art["조번호"]
                if art["삭제"]:
                    통계["삭제컷"] += 1;  continue
                if RE_첨부.match(조번호 or "") and 표인가(art["본문"]):
                    통계["첨부컷"] += 1;  continue      # 박스표만 자른다 (위 주석)
                # 🔴 산문 부속은 적재하되 **검색 진입점이 아니다** (2026-08-31 실측).
                #    붙임 청크는 인덱스의 2.8%(574/20,525)인데 top-5 의 49.1% 를 차지하고
                #    정답이었던 건 1건뿐이었다 — 17배 과대표집이다.
                #    원인은 길이가 아니라 **표를 세로로 읽어 컬럼이 뒤섞인 것**이다
                #    ("비목 내용 • 사업계획서 상의… 정의 구매하는 비용"). 이미 900토큰으로
                #    쪼개져 있어(79%가 2,000자 이하) 더 분할해도 안 풀린다.
                #    폐포전용으로 돌리면 전 구간이 오른다: hit@1 24.3->28.6 · hit@5 48.6->50.0
                #    · hit@50 70.0->71.4. 근거가 필요하면 refs 폐포로 도달한다 (RAG.md §4-3).
                부속 = bool(RE_첨부.match(조번호 or ""))
                if 조번호 in 범위밖:
                    continue                        # 위에서 이미 셌다
                if not (art["본문"] or "").strip():
                    continue

                적용 = TAT.적용대상_of(doc_id, 조번호, 태그)
                if 적용 is None:
                    # 태깅 대상 안인데 아직 못 가른 조. 인덱스에 올리지 않는다 —
                    # 주관기관 규정이 창업기업 판정에 섞이는 것이 더 나쁘다.
                    통계["미결컷"] += 1;  continue

                조각 = 병합(분할(tok, art["본문"]))
                if len(조각) > 1:
                    통계["분할조"] += 1
                h = 헤더(layer, 사업, doc_id, 장.get(조번호, ""), 조번호, art["조제목"])
                for 항호, txt in 조각:
                    rows.append((doc_id, art["article_id"], layer, 기관, pq, ver, status,
                                 "폐포전용" if 부속 else scope,
                                 조번호, art["조제목"], 항호, art["페이지"],
                                 사업, 적용, txt))
                    임베딩입력.append(f"{h}\n{txt}")
                    통계["청크"] += 1

        # ── 게이트: 1,024토큰 초과 0건 ───────────────────────────────────────
        print("게이트: 헤더+본문 토큰 검사...", flush=True)
        길이 = []
        for i in range(0, len(임베딩입력), 500):
            길이 += 토큰수(tok, 임베딩입력[i:i + 500])
        초과 = [i for i, n in enumerate(길이) if n > GATE_TOK]
        최대 = max(길이) if 길이 else 0
        평균 = sum(길이) / len(길이) if 길이 else 0
        print(f"  최대 {최대} · 평균 {평균:.0f} · 초과 {len(초과)}건")
        if 초과:
            for i in 초과[:5]:
                print(f"    {길이[i]}토큰  {rows[i][0][:40]} {rows[i][8]} {rows[i][10]}")
            sys.exit("🔴 게이트 실패 — 초과 청크가 있다. 임베딩하면 꼬리가 잘린다.")

        print("\n" + " · ".join(f"{k} {v:,}" for k, v in 통계.items()))

        if a.dry_run:
            print("\n--dry-run — DB 를 쓰지 않았다.")
            return

        with conn.cursor() as cur:
            cur.execute("TRUNCATE corpus.chunks RESTART IDENTITY CASCADE;")
            cur.executemany("""
                INSERT INTO corpus.chunks
                  (doc_id, article_id, layer, 기관ID, parse_quality, version, status,
                   retrieval_scope, 조번호, 조제목, 항호, 페이지, 사업명, 적용대상, text)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, rows)
        conn.commit()
        n = conn.execute("SELECT count(*) FROM corpus.chunks").fetchone()[0]
        print(f"\ncorpus.chunks 적재 {n:,}건")

        # 임베딩 입력을 chunk_id 순서로 내보낸다. (d) 가 이 순서 그대로 UPDATE 한다.
        ids = [r[0] for r in conn.execute(
            "SELECT chunk_id FROM corpus.chunks ORDER BY chunk_id").fetchall()]
        assert len(ids) == len(임베딩입력), f"순서 불일치 {len(ids)} vs {len(임베딩입력)}"
        OUT_JSONL.parent.mkdir(parents=True, exist_ok=True)
        with OUT_JSONL.open("w", encoding="utf-8") as f:
            for cid, s, n_tok in zip(ids, 임베딩입력, 길이):
                f.write(json.dumps({"chunk_id": cid, "text": s, "tok": n_tok},
                                   ensure_ascii=False) + "\n")
        mb = OUT_JSONL.stat().st_size / 1e6
        print(f"임베딩 입력: {OUT_JSONL.relative_to(ROOT)}  ({mb:.1f}MB)")

    print(f"\n완료 — {time.time() - t0:.0f}초")


if __name__ == "__main__":
    main()
