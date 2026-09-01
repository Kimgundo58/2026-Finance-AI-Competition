# -*- coding: utf-8 -*-
"""Stage 0 오케스트레이터 : manifest 라우팅 → 파싱 → 검증 → DB 적재.

실행:  python scripts/stage0_ingest.py
"""
from __future__ import annotations
import io, json, os, re, sys, time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from index_guard import reject_reason

import psycopg
from stage0_extract import extract
from stage0_articles import split_articles, validate, find_xrefs, sanitize

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")

# ── 메타데이터 유도 규칙 ─────────────────────────────────────────
사업_키워드 = [
    ("예비창업패키지", "예비창업패키지"),
    ("초기창업패키지", "초기창업패키지"),
    ("창업도약패키지", "창업도약패키지"),
]

def derive_사업명(fname: str, layer: str) -> list[str] | None:
    hits = [v for k, v in 사업_키워드 if k in fname]
    if hits:
        return hits
    if layer in ("L1", "L2"):
        return None                      # 전 사업 공통
    return None


def derive_기관ID(path: str, layer: str) -> str | None:
    if layer != "L4":
        return None
    base = Path(path).stem
    parts = base.split("_")
    if base.startswith("L4_") and len(parts) >= 2:
        return parts[1]                  # L4_강원대_... → 강원대
    for pre in ("AC", "BI", "TP", "혁신센터"):
        if base.startswith(pre + "_") and len(parts) >= 2:
            return parts[1]
    return parts[0] if parts else None


def derive_domain(path: str, layer: str) -> str:
    if layer in ("L1", "L2", "L3"):
        return "창업지원사업"
    if layer == "사례":
        return "사례"
    name = Path(path).name
    if "/대학/" in path.replace("\\", "/") or "대학" in path:
        if "대학혁신" in name:
            return "대학혁신지원사업"
        if any(k in name for k in ("연구비", "회계", "산학협력단")):
            return "연구비"
    return "기관운영"


def derive_roles(f: dict) -> list[str]:
    r = f.get("role")
    if isinstance(r, list):
        return r
    return [r] if r else []


def main():
    t0 = time.time()
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    files = manifest["files"]

    report = {"성공": [], "실패": [], "플래그": [], "건너뜀": []}
    total_articles = 0

    # ── 처리 대상 선별 (§2.1 라우팅) ─────────────────────────────
    # archive 전용 문서까지 파싱하면 낭비다.
    # (권익위재결례집 2개 = 1,674p 는 인덱싱 대상이 아니므로 제외)
    # case_index / compare_analysis 는 넣지 않는다:
    #   - 실제로 쓰는 사례집은 index:true 로 이미 잡힌다
    #   - compare_analysis 는 전부 L4 라 layer 조건으로 잡힌다
    #   → 넣으면 권익위재결례집 2개(1,674p, archive)를 헛되이 파싱하게 된다
    NEEDED_ROLES = {"judgment_index", "rule_source", "diff_only", "golden_set"}
    targets = []
    for f in files:
        roles = set(derive_roles(f))
        if f.get("index") or f.get("layer") == "L4" or (roles & NEEDED_ROLES):
            targets.append(f)
        else:
            report["건너뜀"].append((Path(f["file"]).stem, f"불필요({','.join(roles) or 'no-role'})"))
    print(f"처리 대상 {len(targets)} / 전체 {len(files)} 문서\n", flush=True)

    with psycopg.connect(DSN) as conn:
        conn.execute("TRUNCATE doc_articles, chunks, case_chunks, xref_mismatch, documents CASCADE;")
        conn.commit()

        for n, f in enumerate(targets, 1):
            rel = f["file"]
            path = ROOT / rel
            layer = f.get("layer") or "사례"
            roles = derive_roles(f)
            doc_id = Path(rel).stem
            print(f"[{n:>2}/{len(targets)}] {doc_id[:58]}", flush=True)

            if not path.exists():
                report["실패"].append((doc_id, "파일없음"))
                continue
            if path.suffix.lower() not in (".xml", ".pdf", ".hwp", ".txt"):
                report["건너뜀"].append((doc_id, f"형식:{path.suffix}"))
                continue

            # 인덱싱 대상 판정
            index_target = bool(f.get("index"))
            # 골든셋 격리 — 단 case_index 를 겸하는 문서(연구재단 QA사례집)는
            # 파일 통째로 빼면 안 된다. Q&A 단위로 홀드아웃을 떼는 것이 맞다(§8.1).
            # ⚠️ 골든셋 확정 후 해당 Q&A 를 case_chunks 에서 삭제할 것.
            if "golden_set" in roles and "case_index" not in roles:
                index_target = False
            # 최종 게이트 — 경로·레이어 블랙리스트(scripts/index_guard.py).
            # 여기까지 오면 위 조건들과 무관하게 무조건 거부된다.
            if index_target:
                why = reject_reason(rel, layer)
                if why is not None:
                    index_target = False
                    report["플래그"].append((doc_id, f"인덱스 거부: {why}"))

            try:
                kind, payload = extract(path)
                if kind == "articles":
                    arts = [{**a, "본문": sanitize(a["본문"]),
                             "조제목": sanitize(a.get("조제목") or "") or None,
                             "조번호_int": _num(a["조번호"])} for a in payload]
                    strategy, raw_text = "xml_native", "\n".join(a["본문"] for a in arts)
                else:
                    raw_text, page_offsets = payload
                    arts, strategy = split_articles(raw_text, page_offsets)

                v = validate(arts, strategy)
                if not v["ok"]:
                    report["실패"].append((doc_id, f"조_추출_0건 ({strategy})"))
                    continue

                quality = "high" if strategy == "xml_native" else v["quality"]

                conn.execute("""
                    INSERT INTO documents
                      (doc_id, layer, domain, 기관ID, doc_type, version, 시행일,
                       status, parse_quality, src_path, index_target)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (doc_id, layer, derive_domain(rel, layer), derive_기관ID(rel, layer),
                      f.get("doc_type"), f.get("version"), _date(f.get("시행일")),
                      f.get("status") or "reference", quality,
                      rel, index_target))

                rows = [(doc_id, a["조번호"], a.get("조제목"), a.get("조번호_int"),
                         a["본문"], a.get("페이지")) for a in arts]
                with conn.cursor() as cur:
                    cur.executemany("""
                        INSERT INTO doc_articles (doc_id, 조번호, 조제목, 조번호_int, 본문, 페이지)
                        VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
                    """, rows)

                # V6 크로스 레퍼런스
                for x in find_xrefs(raw_text):
                    conn.execute("""
                        INSERT INTO xref_mismatch (src_doc_id, 참조문자열, 상태)
                        VALUES (%s,%s,'mismatch')
                    """, (doc_id, x["참조문자열"]))

                conn.commit()          # 문서 단위 커밋 — 진행 상황을 밖에서 볼 수 있게
                total_articles += len(arts)
                report["성공"].append((doc_id, layer, strategy, len(arts), quality))
                if v["flags"]:
                    report["플래그"].append((doc_id, v["flags"]))
                print(f"        → {len(arts)}개 조 ({strategy}, {quality})", flush=True)

            except Exception as e:
                conn.rollback()
                report["실패"].append((doc_id, f"{type(e).__name__}: {e}"))
                print(f"        ✗ {type(e).__name__}: {str(e)[:70]}", flush=True)

        conn.commit()

    _print_report(report, total_articles, time.time() - t0)
    (ROOT / "scripts" / "_stage0_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def _num(조번호: str):
    m = re.search(r"(\d+)", 조번호 or "")
    return int(m.group(1)) if m else None


def _date(v):
    if not v:
        return None
    s = str(v).replace(".", "-").replace("/", "-")
    m = re.match(r"(\d{4})-?(\d{2})-?(\d{2})", s)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def _print_report(r, total, secs):
    print("=" * 68)
    print(f"Stage 0 완료  —  {secs:.1f}초")
    print("=" * 68)
    print(f"성공 {len(r['성공'])}개 문서 / 총 {total}개 조")
    print(f"실패 {len(r['실패'])} · 건너뜀 {len(r['건너뜀'])} · 플래그 {len(r['플래그'])}")
    if r["실패"]:
        print("\n[실패]")
        for d, why in r["실패"]:
            print(f"  ✗ {d[:55]:<55} {why[:60]}")
    if r["플래그"]:
        print("\n[플래그 — 사람 확인 필요]")
        for d, fl in r["플래그"]:
            print(f"  ! {d[:55]:<55} {', '.join(fl)[:70]}")


if __name__ == "__main__":
    main()
