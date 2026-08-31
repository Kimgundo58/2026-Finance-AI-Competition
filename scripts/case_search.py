# -*- coding: utf-8 -*-
"""판단불가 경로 — 사례 조회 (`Agent.md` §7 · `RAG.md` §2-2).

**사례는 판정에 영향을 주지 않는다.** 판정이 이미 `판단불가` 로 확정된 뒤에만
`corpus.case_chunks` 를 조회한다. 이건 프롬프트로 부탁하는 규칙이 아니라
**함수가 거부하는 규칙**이다 (F5):

    유사사례(cur, 질문, 판정="가능")     -> RuntimeError
    유사사례(cur, 질문, 판정="조건부")   -> RuntimeError
    유사사례(cur, 질문, 판정="판단불가") -> [ … ]

왜 이렇게까지 하나
──────────────────
`case_chunks` 는 B등급이다. R&D·보조금 도메인의 남의 규정으로 만들어진 사례라
창업지원사업 판정의 근거가 될 수 없다. 그런데 벡터가 잘 붙는다 — "이 돈 써도 되나"
류 질문은 도메인이 달라도 표면이 닮는다. 그래서 조회 자체를 막지 않으면
언젠가 누군가 "근거가 모자라니 사례라도 붙이자" 를 한다. 그 순간 인용 신뢰가
무너진다. 물리 분리(다른 테이블) + 함수 게이트(이 파일)가 짝이다.

🔴 이 모듈은 `corpus.chunks` 를 SELECT 하지 않는다. 반대로 `retrieve.검색()` 은
   `case_chunks` 를 SELECT 하지 않는다. 두 방향 다 지켜야 분리가 성립한다.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

판단불가 = "판단불가"

# 화면 표시 라벨. 사례가 근거로 읽히는 것을 막는 마지막 방어선이라 상수로 둔다.
라벨 = "귀하의 사업에 적용되지 않습니다"
라벨_설명 = ("다른 제도(국가연구개발사업·국고보조금)의 사례입니다. "
             "판정 근거가 아니라 참고 자료이며, 담당자 확인이 필요합니다.")

# 이 아래는 붙여봐야 헛짚은 사례다. 판단불가에 엉뚱한 사례를 붙이면
# "비슷한 걸 찾았다"는 인상만 주고 실제로는 오도한다 — 차라리 0건이 낫다.
#
# 실측 (2026-09-01 · 골든셋 77문항 × 적재분 193사례, top-1 코사인):
#     본세트 50   최소 0.446 · 중앙 0.555 · 최대 0.682
#     적대적 27   최소 0.490 · 중앙 0.565 · 최대 0.672
#
# 🔴 **77문항 전부가 0.70 아래다.** 정답이 그대로 뜨는 일은 없다 — F1 의 사후 확인.
#
# 🔴 임계를 양쪽에서 재봤다. 진짜 질문 77 vs **명백히 범위 밖인 음성 질문 10건**
#    (고양이 사료 · 로또 · 자녀 학원비 · 오늘 날씨 …):
#
#      임계    진짜 질문 유지     음성 오부착
#      0.50    71/77 (92%)      8/10 (80%)
#      0.55    47/77 (61%)      3/10 (30%)   <- 현재
#      0.60    26/77 (34%)      1/10 (10%)
#      0.65    11/77 (14%)      0/10 ( 0%)
#      0.70     0/77 ( 0%)      0/10 ( 0%)
#
#    **분리되는 지점이 없다.** 진짜 질문과 헛소리가 같은 띠(0.5~0.6)에 겹쳐 있다.
#    당연하다 — 적재분이 전량 R&D·보조금 도메인이라 창업 질문도 "잘 안 맞는" 쪽이고
#    헛소리도 "잘 안 맞는" 쪽이다. 코사인은 이 둘을 가르는 축이 아니다.
#
#    그래서 **숫자를 옮기지 않았다.** 옮겨봐야 한쪽을 버리고 다른 쪽을 얻을 뿐이다.
#    고칠 것은 임계가 아니라 상호작용이다 — 자동 노출 대신 "비슷한 사례 보기" 를
#    사용자가 눌러서 열게 하면 오부착의 비용이 사라진다. UI 는 오늘 범위 밖이라
#    `결과보고.md` 에 적어 두고 코드는 그대로 둔다 (`0831_최종구현.md` §10).
최소유사도 = 0.55


# ── 임베딩 ───────────────────────────────────────────────────────────────────
# C 의 `retrieve` 가 이미 KURE-v1 을 프로세스에 상주시킨다. 같은 프로세스에서
# 두 번 올리면 CPU 메모리를 2GB 더 먹으므로 있으면 빌려 쓴다. 없으면 자체 로드 —
# 이 모듈 단독 테스트(F7 회귀)가 C 에 묶이면 안 된다.
_모델 = None


def _임베딩(질문: str) -> str:
    """질문 -> pgvector 리터럴. 정규화 벡터라 `1 - (a <=> b)` 가 코사인이다."""
    try:
        import retrieve
        return retrieve.임베딩(질문)
    except Exception:
        pass
    global _모델
    if _모델 is None:
        from sentence_transformers import SentenceTransformer
        m = SentenceTransformer("nlpai-lab/KURE-v1", device="cpu")
        m.max_seq_length = 1024
        _모델 = m
    v = _모델.encode([질문], normalize_embeddings=True, convert_to_numpy=True,
                     show_progress_bar=False)[0]
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


SQL = """
SELECT case_id, doc_id, 출처도메인, question, answer,
       1 - (embedding <=> %s::extensions.vector(1024)) AS 유사도
  FROM corpus.case_chunks
 WHERE embedding IS NOT NULL
 ORDER BY embedding <=> %s::extensions.vector(1024)
 LIMIT %s
"""


def 유사사례(cur, 질문: str, *, k: int = 3, 판정: str) -> list[dict]:
    """판정이 `판단불가` 일 때만 유사 사례 상위 k 건.

    🔴 `판정` 은 키워드 전용 필수 인자다. 기본값을 주지 않는 게 핵심 —
       호출자가 판정을 안 넘기면 TypeError 로 즉시 터진다. 기본값이 있으면
       "깜빡하고 안 넘김" 이 조용히 통과한다.

    반환 (0건이어도 None 이 아니라 빈 리스트):
        [{"case_id", "doc_id", "출처도메인", "question", "answer",
          "유사도", "라벨", "라벨_설명", "등급"}]
    """
    if 판정 != 판단불가:
        raise RuntimeError(
            f"사례 조회는 판정이 '{판단불가}' 로 확정된 뒤에만 허용된다 (판정='{판정}'). "
            "사례는 B등급이라 판정 근거가 될 수 없다 — Agent.md §7 · RAG.md §2-2."
        )
    if not (질문 or "").strip():
        return []

    벡터 = _임베딩(질문)
    cur.execute(SQL, (벡터, 벡터, max(1, int(k))))
    out = []
    for case_id, doc_id, 도메인, q, ans, sim in cur.fetchall():
        sim = float(sim)
        if sim < 최소유사도:
            continue
        out.append({"case_id": case_id, "doc_id": doc_id, "출처도메인": 도메인,
                    "question": q, "answer": ans, "유사도": round(sim, 4),
                    "라벨": 라벨, "라벨_설명": 라벨_설명, "등급": "B"})
    return out


# ── 자기 점검 (F5·F7) ────────────────────────────────────────────────────────
def _selftest() -> int:
    """`판정` 게이트와 분리 불변식을 스스로 검사한다. 회귀에서 이걸 돌린다."""
    import psycopg
    dsn = os.environ.get("SUDDOE_DSN",
                         "postgresql://postgres:devpw@localhost:5432/suddoe")
    실패 = 0
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        # ① 게이트 — 판단불가 아닌 판정은 전부 거부
        for p in ("가능", "조건부", "", "판단 불가", "불가"):
            try:
                유사사례(cur, "노트북을 사도 되나요", 판정=p)
                print(f"  🔴 실패 — 판정='{p}' 인데 조회가 통과했다"); 실패 += 1
            except RuntimeError:
                pass
        print("  ✓ 게이트 — 판단불가가 아닌 판정 5종 전부 RuntimeError")

        # ② 판정 인자 누락은 TypeError
        try:
            유사사례(cur, "노트북")            # type: ignore[call-arg]
            print("  🔴 실패 — 판정 인자 없이 호출이 통과했다"); 실패 += 1
        except TypeError:
            print("  ✓ 판정 인자 누락 -> TypeError")

        # ③ 정상 경로
        r = 유사사례(cur, "학회 참가비를 사업비로 써도 되나요", k=3, 판정=판단불가)
        print(f"  ✓ 판단불가 경로 {len(r)}건")
        for x in r:
            print(f"      {x['유사도']:.3f} [{x['출처도메인']}] {x['question'][:56]}")
        if any(x["라벨"] != 라벨 for x in r):
            print("  🔴 실패 — 라벨 누락"); 실패 += 1

        # ④ 분리 불변식 — case_chunks 의 doc_id 는 판정 인덱스에 없어야 한다
        cur.execute("""
            SELECT DISTINCT c.doc_id FROM corpus.case_chunks c
              JOIN corpus.documents d USING (doc_id)
             WHERE d.index_target IS TRUE
        """)
        누수 = [r[0] for r in cur.fetchall()]
        if 누수:
            print("  🔴 실패 — 판정 인덱스 대상 문서가 사례로도 들어가 있다:", 누수)
            실패 += 1
        else:
            print("  ✓ 분리 — case_chunks 문서 중 index_target=True 0건")

        # ⑤ 오염 — 오염검사에서 제외된 문서가 적재돼 있으면 안 된다
        import json
        from pathlib import Path
        audit = Path(ROOT) / "_work" / "_F_오염검사.json"
        if audit.exists():
            허용 = set(json.loads(audit.read_text(encoding="utf-8"))["최종채택"])
            cur.execute("SELECT DISTINCT doc_id FROM corpus.case_chunks")
            적재 = {r[0] for r in cur.fetchall()}
            벗어남 = 적재 - 허용
            if 벗어남:
                print("  🔴 실패 — 오염검사 통과 목록 밖의 문서가 적재됨:", 벗어남)
                실패 += 1
            else:
                print(f"  ✓ 오염 — 적재 문서 {len(적재)}종이 전부 통과 목록 안")
        else:
            print("  ⚠️  오염검사 보고서가 없다 — stage2_cases.py check 를 먼저 돌릴 것")
            실패 += 1
    return 실패


def _실코드(원본: str) -> str:
    """소스에서 **실행되는 것**만 남긴다 — 주석과 독스트링을 뺀다.

    분리 검사는 "이 파일이 저 테이블을 읽는가" 를 묻는다. 그런데 이 모듈의
    독스트링에는 *"`corpus.chunks` 를 SELECT 하지 않는다"* 라고 **적혀 있다.**
    문자열을 통째로 훑으면 그 선언 문장이 위반으로 잡힌다 (실제로 잡혔다).
    `#` 주석만 걷어내는 것으로는 부족하다 — 독스트링은 주석이 아니라 식이다.

    그렇다고 문자열을 전부 빼면 안 된다. SQL 이 문자열 상수에 들어 있어서
    그것까지 빼면 검사가 아무것도 못 본다. 그래서 **독스트링만** 골라 뺀다.
    """
    import ast
    try:
        tree = ast.parse(원본)
    except SyntaxError:
        # 잘라낸 조각이라 파싱이 안 될 수 있다. 주석만 걷고 넘어간다
        return "\n".join(l for l in 원본.splitlines()
                         if not l.lstrip().startswith("#"))
    독스트링 = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                독스트링.add(id(body[0].value))
    조각 = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in 독스트링:
                조각.append(node.value)
        elif isinstance(node, ast.Attribute):
            조각.append(node.attr)
        elif isinstance(node, ast.Name):
            조각.append(node.id)
    return "\n".join(조각)


# ── F7 : 사례 조회는 판정에 영향을 주지 않는다 ───────────────────────────────
def _f7() -> int:
    """"조회했더니 판정이 달라졌다" 가 불가능함을 세 층에서 본다.

    판정 파이프라인(A 의 `orchestrate`)을 두 번 돌려 비교하는 것만으로는 모자란다 —
    그건 "이번엔 안 바뀌었다" 이지 "바뀔 수 없다" 가 아니다. 구조로 본다.
    """
    import re
    from pathlib import Path
    실패 = 0
    S = Path(ROOT)

    # ① 소스 분리 — 두 방향 다 본다
    #    case_search 가 chunks 를 읽으면 사례가 판정 근거로 샌다.
    #    retrieve 가 case_chunks 를 읽으면 판정이 사례를 근거로 삼는다.
    # 🔴 패턴을 조각으로 만든다. 소스에 리터럴로 적으면 **이 검사기 자신이**
    #    검사 대상 파일 안의 금지 문자열이 되어 스스로에게 걸린다 (실제로 걸렸다).
    _chunks = r"corpus\." + "chunks"
    _cases = r"corpus\." + "case_chunks"

    for 파일, 금지, 왜, 판정경로만 in [
        ("case_search.py", _chunks, "사례 검색이 판정 인덱스를 읽는다", True),
        ("retrieve.py", _cases, "판정 검색이 사례 인덱스를 읽는다", False),
        ("assemble_context.py", "case_chunks|유사사례", "컨텍스트 조립에 사례가 들어간다", False),
    ]:
        p = S / 파일
        if not p.exists():
            print(f"  ⚠️  {파일} 없음 — 건너뜀 (해당 세션 작업 전)")
            continue
        원본 = p.read_text(encoding="utf-8")
        # 판정 중 실제로 실행되는 부분만 본다. 파일 하단의 자기 점검 하네스는
        # 판정 경로가 아니고, 검증을 위해 두 테이블을 다 읽어야 한다.
        if 판정경로만:
            원본 = 원본.split("# ── 자기 점검")[0]
        코드 = _실코드(원본)
        if re.search(금지, 코드):
            print(f"  🔴 실패 — {파일} 에 `{금지}` 가 있다: {왜}"); 실패 += 1
        else:
            print(f"  ✓ 분리 — {파일} 에 `{금지}` 없음")

    # ② 게이트 — 4-way 중 판단불가 외 전부 거부. ①과 달리 실행으로 본다
    import psycopg
    dsn = os.environ.get("SUDDOE_DSN",
                         "postgresql://postgres:devpw@localhost:5432/suddoe")
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for p in ("가능", "조건부", "불가"):
            try:
                유사사례(cur, "테스트", 판정=p)
                print(f"  🔴 실패 — 판정='{p}' 조회 통과"); 실패 += 1
            except RuntimeError:
                pass
        print("  ✓ 게이트 — 4-way 중 판단불가 외 3종 전부 거부")

        # ③ 읽기 전용 — 조회가 어떤 테이블도 바꾸지 않는다
        before = {}
        for t in ("corpus.case_chunks", "corpus.chunks", "tenant.decisions",
                  "corpus.rules"):
            try:
                before[t] = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            except Exception:
                pass
        for q in ("노트북 구입", "학회 참가비", "외주용역 선급금", "임차료"):
            유사사례(cur, q, k=3, 판정=판단불가)
        conn.rollback()
        바뀜 = [t for t, n in before.items()
                if conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] != n]
        if 바뀜:
            print("  🔴 실패 — 사례 조회가 테이블을 바꿨다:", 바뀜); 실패 += 1
        else:
            print(f"  ✓ 읽기전용 — 조회 4회 후 {len(before)}개 테이블 행수 불변")

        # ④ 결정성 — 같은 질문은 같은 사례. 판정 재현성(Agent.md §6)의 일부다
        a = 유사사례(cur, "학회 참가비를 사업비로 써도 되나요", k=3, 판정=판단불가)
        b = 유사사례(cur, "학회 참가비를 사업비로 써도 되나요", k=3, 판정=판단불가)
        if [x["case_id"] for x in a] != [x["case_id"] for x in b]:
            print("  🔴 실패 — 같은 질문에 다른 사례"); 실패 += 1
        else:
            print(f"  ✓ 결정성 — 동일 질문 2회 결과 일치 ({len(a)}건)")
    return 실패


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace", line_buffering=True)
    모드 = sys.argv[1] if len(sys.argv) > 1 else "--all"
    실패 = 0
    if 모드 in ("--all", "--selftest"):
        print("① case_search 자기 점검 (F5)")
        실패 += _selftest()
    if 모드 in ("--all", "--f7"):
        print("\n② 판정 무영향 회귀 (F7)")
        실패 += _f7()
    print(f'\n{"🔴 실패 " + str(실패) + "건" if 실패 else "✅ 전부 통과"}')
    sys.exit(1 if 실패 else 0)
