# -*- coding: utf-8 -*-
"""corpus.documents.extraction 재태깅 — 실제 추출 경로에 맞춘다.

## 왜

`extraction` 은 "이 문서의 텍스트를 어떻게 얻었나" 를 담고, **신뢰등급 A/B 산정의
근거 컬럼**이다 (`Agent.md` 신뢰등급, `db/init/01_schema.sql` documents).

    native  = PDF/XML 텍스트 레이어 그대로              -> A등급 인용 가능
    dedupe  = 문자중복 레이어를 dedupe_chars() 로 해소   -> A등급 인용 가능
    hancom  = HWP -> 한컴오피스 PDF 변환                 -> A등급 인용 가능
    vlm     = 스캔 이미지 판독                           -> 🔴 A등급 금지 + 경고 강제

그런데 적재기(`load_db.py`)가 이 값을 계산하지 않아 283건 전부 DDL 기본값 `native`
로 들어가 있었다. dedupe·hancom·vlm 이 0건.

⚠️ **사고가 아니라 정합성 문제다.** 스캔 문서 3건은 셋 다 `index_target=false` 라
청크가 0개이고, 검색에 들어간 적이 없다. 이미 `index_target` 이 막고 있다.
이 스크립트는 그 방어선을 `extraction` 으로 한 겹 더 두는 것이지, 유출을 막는 게 아니다.

## 판정 규칙 (우선순위 순 — 위에서 걸리면 아래는 안 본다)

1. **vlm** — `2026_Finance_DATA_FOR_RAG/_scan_inventory.json` 등재 문서.
   그 파일의 판정 기준은 "앞·중간·뒤 3쪽 표본의 쪽당 평균 문자수 < 60 이면 스캔 전용".
   등재 45건 중 `corpus.documents` 에 있는 것은 3건뿐이다 (나머지는 PMS 매뉴얼·별표 등
   미적재분). 스캔본은 텍스트를 어느 경로로 얻었든 A등급 인용이 금지돼야 하므로
   **가장 위에 둔다** — 안전한 방향으로만 틀리게 만드는 배치다.

   🔴 단, 지금 이 3건에 **VLM 판독본은 존재하지 않는다** (2026-08-31 실측):
       [주관기관]전자협약~사업비집행_v.1.2        저장 3,370자 = native 재추출 3,370자 (동일)
       창업사업화 지원사업 부정행위 방지 사례집    저장 0자 / native 추출은 개행 61자뿐
       2026년 재도전성공패키지 세부관리기준(11차)  저장 0자 / native 추출은 쪽번호 53자뿐
   즉 이 태그는 "판독했다" 는 기록이 아니라 **"판독 없이는 인용 금지" 라는 가드**다.
   `native`(= A등급 인용 가능) 로 두는 쪽이 명백히 위험하므로 vlm 으로 간다.
   실제 판독을 하게 되면 이 주석과 `_scan_inventory.json` 의 A등급 방침을 같이 갱신한다.

2. **dedupe** — `pdftext.extract_meta()` 가 돌려주는 dedupe 플래그. 하드코딩이 아니라
   **매 실행 실측**한다. 판정식은 `dup_ratio(probe) > 0.35`, probe 는 `pages[min(4,n-1)]`
   한 쪽뿐이라 `max_pages=5` 로 잘라 불러도 **같은 쪽을 본다** — 결과 동일, 속도 10배.
   (4건 교차검증: max5 == full, 15.8s -> 1.5s)

   실측 결과 2건. 분리가 깨끗하다 (0.500 / 0.507  vs  나머지 전부 <= 0.017):
       L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223   dup=0.500  index_target=true
       창업도약패키지 지원사업 세부관리기준(2022년)                dup=0.507  index_target=false
   `CLAUDE.md` 가 적어둔 중복 3건 중 세 번째(초기창업 질의응답집 별첨4)는 정답셋이라
   검색 대상에 넣기 금지 대상 — `corpus.documents` 에 없다. 그래서 3이 아니라 2가 맞다.

3. **hancom** — `_hwp변환/` 아래 경로. `convert_hwp.py` 가 원본 트리를 그대로 미러링하므로
   `_hwp변환/<원본경로>.pdf` 옆에 같은 이름의 `.hwp`/`.hwpx` 가 있어야 한다.
   **경로 접두사만 믿지 않고 원본 존재까지 확인**한다 (23/23 확인, .hwp 22 + .hwpx 1).
   조달분(L1·L2) 한컴 1회 수동 변환 방침은 `CLAUDE.md` 확정 원칙.

4. **native** — 위 어디에도 안 걸리는 것. 법령 XML 226건, 텍스트 레이어 PDF, .txt 2건.

`documents.extraction` 의 CHECK 는 ('native','dedupe','hancom','vlm') 4종이다.
`hwpx`/`hwp` 는 L3 업로드(`tenant`) 전용이라 여기서는 나올 수 없다.

우선순위상 vlm > dedupe > hancom 이지만 **실제 충돌은 0건**이다 (스캔 3건은 `_hwp변환/`
밖에 있고 dedupe 2건도 마찬가지). 순서는 앞으로 겹칠 때를 위한 규칙일 뿐이다.

## parse_quality

vlm 판정 문서는 `_scan_inventory.json` 방침대로 `parse_quality='low'` 도 건다.
`corpus.chunks` 에 사본이 있으므로 둘 다 갱신한다.

🔴 **판정 검색 필터가 `parse_quality='high'` 라, 이 값을 내리면 해당 청크가 검색에서
빠진다.** 그래서 내리기 전에 "지금 high 인데 low 로 내려갈 청크" 를 세고,
**0이 아니면 아무것도 쓰지 않고 멈춘다.** (현재 3건 모두 청크 0개 -> 0)

`corpus.chunks` 에는 `extraction` 컬럼이 없다. documents 만 갱신하면 된다.

## 실행

    python scripts/archive/indexing/retag_extraction.py            # dry-run (기본)
    python scripts/archive/indexing/retag_extraction.py --apply    # 실제 UPDATE

멱등하다. 두 번 돌려도 두 번째는 변경 0건.
"""
from __future__ import annotations

# 🔴 2026-09-05 scripts/archive/ 이관 — 원래 scripts/ 바로 밑에 있던 파일이라
#    아래(또는 이 파일의 기존 sys.path 계산)는 scripts/ 바로 밑 기준으로 짜여 있다.
#    이관으로 깊이가 늘어나 깨지므로, `scripts/_lib` 을 찾을 때까지 위로 걸어 올라가
#    scripts/ 와 프로젝트 루트를 sys.path 맨 앞에 다시 건다.
import os as _os_이관, sys as _sys_이관
_p_이관 = _os_이관.path.dirname(_os_이관.path.abspath(__file__))
while not _os_이관.path.isdir(_os_이관.path.join(_p_이관, "_lib")):
    _parent_이관 = _os_이관.path.dirname(_p_이관)
    if _parent_이관 == _p_이관:
        break
    _p_이관 = _parent_이관
if _p_이관 not in _sys_이관.path:
    _sys_이관.path.insert(0, _p_이관)
if _os_이관.path.dirname(_p_이관) not in _sys_이관.path:
    _sys_이관.path.insert(0, _os_이관.path.dirname(_p_이관))
# 🔴 archive 내부에서 카테고리를 넘나드는 import(예: index_guard, stage0_run)가
#    있어 scripts/archive/ 의 모든 하위 폴더도 같이 건다.
_archive_이관 = _os_이관.path.join(_p_이관, "archive")
if _os_이관.path.isdir(_archive_이관):
    for _d_이관 in _os_이관.listdir(_archive_이관):
        _full_이관 = _os_이관.path.join(_archive_이관, _d_이관)
        if _os_이관.path.isdir(_full_이관) and _full_이관 not in _sys_이관.path:
            _sys_이관.path.insert(0, _full_이관)


import argparse
import io
import json
import os
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "scripts" / "_lib").is_dir())  # 🔴 2026-09-05 archive 이관 — 깊이 무관 계산으로 교체
sys.path.insert(0, str(ROOT / "scripts"))

import psycopg          # noqa: E402
import pdftext          # noqa: E402  🔴 PDF 텍스트는 반드시 이 모듈 경유 (CLAUDE.md)

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")
SCAN_INVENTORY = ROOT / "2026_Finance_DATA_FOR_RAG" / "_scan_inventory.json"
HWP_MIRROR = "_hwp변환"

# extract_meta 의 dedupe 판정은 pages[min(4,n-1)] 한 쪽만 본다. 5쪽만 읽어도 같은 쪽이다.
DEDUPE_PROBE_PAGES = 5


def scan_paths() -> dict[str, dict]:
    """_scan_inventory.json 등재 경로 -> 항목. 키는 '/' 정규화 + 소문자."""
    if not SCAN_INVENTORY.exists():
        raise SystemExit("스캔 대장이 없다: %s" % SCAN_INVENTORY)
    inv = json.loads(SCAN_INVENTORY.read_text(encoding="utf-8"))
    return {d["file"].replace("\\", "/").lower(): d for d in inv["문서"]}


def hancom_origin(rel: str) -> Path | None:
    """`_hwp변환/` 미러 경로면 대응하는 원본 HWP/HWPX 를 돌려준다. 없으면 None."""
    parts = rel.split("/")
    if not parts or parts[0] != HWP_MIRROR:
        return None
    base = ROOT / "/".join(parts[1:])
    for ext in (".hwp", ".hwpx", ".HWP", ".HWPX"):
        cand = base.with_suffix(ext)
        if cand.exists():
            return cand
    return None


def classify(rel: str, scan: dict[str, dict]) -> tuple[str, str]:
    """(extraction, 판정근거) — 위 판정 규칙의 우선순위를 그대로 구현한다."""
    key = rel.lower()
    path = ROOT / rel

    # 1. vlm — 스캔 목록 등재
    if key in scan:
        d = scan[key]
        return "vlm", ("스캔대장 등재 (%s, %d쪽, 표본 %.1f자/쪽)"
                       % (d["등급"].split("(")[0], d["쪽수"], d["표본_평균문자"]))

    # 2. dedupe — 실측
    if path.suffix.lower() == ".pdf" and path.exists():
        try:
            _, meta = pdftext.extract_meta(path, max_pages=DEDUPE_PROBE_PAGES)
        except Exception as e:                                    # noqa: BLE001
            # 읽기 실패는 조용히 native 로 떨어뜨리지 않는다 — 보고 목록에 올린다.
            return "native", "⚠️ PDF 열기 실패(%s) — 근거 없어 native 유지" % type(e).__name__
        if meta["dedupe"]:
            return "dedupe", "pdftext 실측: 문자중복 레이어 감지 (dedupe_chars 적용)"

    # 3. hancom — _hwp변환 미러 + 원본 존재
    origin = hancom_origin(rel)
    if origin is not None:
        return "hancom", "_hwp변환/ 미러 + 원본 %s 확인" % origin.suffix

    if rel.split("/")[0] == HWP_MIRROR:
        return "native", "⚠️ _hwp변환/ 경로인데 원본 HWP 미발견 — 근거 없어 native 유지"

    # 4. native
    if path.suffix.lower() == ".xml":
        return "native", "법령 XML (국가법령정보 API 산출)"
    if not path.exists():
        return "native", "⚠️ 원본 파일 없음 — 근거 없어 native 유지"
    return "native", "텍스트 레이어 그대로"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="실제 UPDATE (없으면 dry-run)")
    args = ap.parse_args()

    scan = scan_paths()

    with psycopg.connect(DSN) as conn:
        rows = conn.execute("""
            SELECT doc_id, layer, index_target, parse_quality, extraction, src_path
            FROM corpus.documents ORDER BY src_path
        """).fetchall()

        plan = []
        for doc_id, layer, itgt, pq, cur, src in rows:
            rel = src.replace("\\", "/")
            new, why = classify(rel, scan)
            plan.append({"doc_id": doc_id, "layer": layer, "index_target": itgt,
                         "parse_quality": pq, "old": cur, "new": new,
                         "why": why, "src": rel})

        changed = [p for p in plan if p["old"] != p["new"]]
        vlm = [p for p in plan if p["new"] == "vlm"]
        flagged = [p for p in plan if p["why"].startswith("⚠️")]

        print("=" * 78)
        print("문서 %d건 / 변경 %d건" % (len(plan), len(changed)))
        for val in ("native", "dedupe", "hancom", "vlm"):
            n = sum(1 for p in plan if p["new"] == val)
            print("   %-7s %4d건" % (val, n))
        print()

        print("[변경 대상]")
        for p in changed:
            print("  %-7s -> %-7s  itgt=%-5s  %s"
                  % (p["old"], p["new"], p["index_target"], p["doc_id"][:52]))
            print("            근거: %s" % p["why"])
        if not changed:
            print("  없음 (이미 정합)")
        print()

        if flagged:
            print("[🔴 근거 없어 native 로 남긴 것 — 사람 확인 필요]")
            for p in flagged:
                print("  %s\n            %s" % (p["doc_id"][:60], p["why"]))
            print()

        # ── parse_quality 영향 통과 조건 ───────────────────────────────────────
        vlm_ids = [p["doc_id"] for p in vlm]
        hit = 0
        if vlm_ids:
            hit = conn.execute("""
                SELECT count(*) FROM corpus.chunks
                WHERE doc_id = ANY(%s) AND parse_quality = 'high'
            """, (vlm_ids,)).fetchone()[0]

        print("[parse_quality 영향]")
        print("  vlm 판정 문서 %d건" % len(vlm_ids))
        for p in vlm:
            print("     itgt=%-5s pq=%-4s %s"
                  % (p["index_target"], p["parse_quality"], p["doc_id"][:50]))
        print("  high -> low 로 내려갈 청크: %d개" % hit)
        if hit:
            print()
            print("🔴 0이 아니다. 판정 검색 필터가 parse_quality='high' 라 이 청크들이")
            print("   검색에서 빠진다. 아무것도 쓰지 않고 멈춘다 — 사람이 판단할 일이다.")
            raise SystemExit(2)
        print("  -> 0. 검색에서 빠지는 청크 없음. 진행 가능.")
        print()

        if not args.apply:
            print("dry-run 이다. 실제로 쓰려면 --apply")
            return

        if changed:
            conn.execute("""
                UPDATE corpus.documents AS d SET extraction = v.new
                FROM (SELECT unnest(%s::text[]) AS doc_id,
                             unnest(%s::text[]) AS new) v
                WHERE d.doc_id = v.doc_id AND d.extraction IS DISTINCT FROM v.new
            """, ([p["doc_id"] for p in changed], [p["new"] for p in changed]))

        n_doc = n_chunk = 0
        if vlm_ids:
            n_doc = conn.execute("""
                UPDATE corpus.documents SET parse_quality = 'low'
                WHERE doc_id = ANY(%s) AND parse_quality <> 'low'
            """, (vlm_ids,)).rowcount
            # 위 통과 조건이 0을 보장하므로 실질 no-op 이다. 그래도 남겨 둔다 —
            # 나중에 vlm 문서가 인덱싱되면 chunks 사본도 같이 내려가야 하고,
            # 그때 이 스크립트가 유일한 갱신 경로가 된다.
            n_chunk = conn.execute("""
                UPDATE corpus.chunks SET parse_quality = 'low'
                WHERE doc_id = ANY(%s) AND parse_quality <> 'low'
            """, (vlm_ids,)).rowcount
        conn.commit()

        print("적용 완료 — extraction %d건 / documents.parse_quality %d건 "
              "/ chunks.parse_quality %d건" % (len(changed), n_doc, n_chunk))

        print()
        print("[검증]")
        for q, label in [
            ("SELECT extraction, count(*) FROM corpus.documents GROUP BY 1 ORDER BY 2 DESC",
             "documents.extraction"),
            ("SELECT count(*), count(embedding) FROM corpus.chunks", "chunks / embedding"),
            ("SELECT count(*) FROM corpus.chunks WHERE parse_quality='high'", "chunks high"),
        ]:
            print("  %-22s %s" % (label, conn.execute(q).fetchall()))


if __name__ == "__main__":
    main()
