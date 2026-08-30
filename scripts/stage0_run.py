# -*- coding: utf-8 -*-
"""Stage 0 오케스트레이터 — 전체 코퍼스 조(條) 분해 → JSON.

`stage0_ingest.py` 를 대체한다. 두 가지가 달라졌다.

1. **`manifest.json` 을 읽지 않는다.** 구 74파일 데이터셋 기준이라 2026-08-12 에 동결됐고
   `CLAUDE.md` 가 "인덱싱 대상 판단에 쓰지 말 것" 으로 못박았는데 오케스트레이터만
   그대로 읽고 있었다. 라우팅 원본을 두 개로 바꿨다:
       L1 법령·행정규칙  ->  `법령 PDF/_law_sources.json` 의 `index` 플래그
       L2·사례·지침류    ->  `2026_Finance_DATA_FOR_RAG/` 실측 스캔
2. **DB 에 넣지 않는다.** 저장소가 Supabase 로 바뀌는 중이라 스키마 확정 전에 적재하면
   두 번 일한다. 조 분해 결과는 저장소 중립이므로 JSON 으로 낸다.

실행:
    python scripts/stage0_run.py              전체
    python scripts/stage0_run.py --only L2    L2 만
    python scripts/stage0_run.py --limit 20   앞 20건 (연습용)
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from index_guard import reject_reason            # noqa: E402
from stage0_extract import extract               # noqa: E402
from stage0_articles import split_articles, validate, sanitize   # noqa: E402

DATASET = ROOT / "2026_Finance_DATA_FOR_RAG"
LAWDIR = ROOT / "법령 PDF"
CONV = ROOT / "_hwp변환"

OUT_ART = DATASET / "_stage0_articles.json"
OUT_REP = DATASET / "_stage0_report.json"

# 데이터셋 안에서 판정 인덱스 대상이 **아닌** 것.
# index_guard 는 절대 규칙(골든셋·L4·archive)만 막는다. 아래는 그 위의 스코프 판단이라
# 게이트가 아니라 여기에 둔다. 사유를 각각 적어 리포트에 남긴다.
DATASET_제외 = {
    "PMS": "PMS 매뉴얼 42건 — 판독 불필요 확정 (서비스 아키텍쳐.md §7-3③)",
    "_정리보류": "중복·범위외 보류분",
}

# HWP 는 파이프라인에 파서를 넣지 않는다 (CLAUDE.md). 한컴 변환본으로 치환한다.
HWP_확장 = {".hwp", ".hwpx"}


def 변환본(p: Path) -> Path | None:
    """HWP -> `_hwp변환/` 아래 같은 상대경로의 PDF."""
    try:
        rel = p.relative_to(ROOT)
    except ValueError:
        return None
    cand = CONV / rel.with_suffix(".pdf")
    return cand if cand.exists() else None


def layer_of(path: Path) -> str:
    s = str(path).replace("\\", "/")
    if "/법령 PDF/" in s or s.endswith(".xml"):
        return "L1"
    if "/중기부/" in s:
        return "L1"
    if "/창진원/" in s:
        return "L2"
    if "/사례집/" in s:
        return "사례"
    if "레퍼런스" in s:
        return "L4"          # 타 기관 규정 — index_guard 가 막는다
    return "기타"


def 대상수집(only: str | None) -> list[dict]:
    """(경로, layer, 출처) 목록. 제외는 사유와 함께 따로 모은다."""
    targets: list[dict] = []
    skipped: list[dict] = []

    # ── L1 법령·행정규칙 : _law_sources.json 의 index 플래그가 원본 ──
    src = json.loads((LAWDIR / "_law_sources.json").read_text(encoding="utf-8"))
    for 규범, meta in src.items():
        rec = {"doc_id": Path(meta["file"]).stem, "규범": 규범, "layer": "L1",
               "norm_type": meta.get("norm_type"), "sources": meta.get("sources")}
        if not meta.get("index"):
            skipped.append({**rec, "사유": f"index=false ({meta.get('index_reason') or '범위 밖'})"})
            continue
        hits = list(LAWDIR.rglob(meta["file"]))
        if not hits:
            skipped.append({**rec, "사유": "파일 없음"})
            continue
        targets.append({**rec, "path": hits[0]})

    # ── L2·중기부·사례 : 데이터셋 실측 스캔 ──────────────────────
    for p in sorted(DATASET.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in {".pdf", ".hwp", ".hwpx", ".xml", ".txt"}:
            continue
        rel = p.relative_to(DATASET)
        top = rel.parts[0]
        rec = {"doc_id": p.stem, "규범": None, "layer": layer_of(p), "path": p}

        why = next((v for k, v in DATASET_제외.items() if top == k), None)
        if why:
            skipped.append({**rec, "path": str(rel), "사유": why})
            continue
        guard = reject_reason(str(p), rec["layer"])
        if guard:
            skipped.append({**rec, "path": str(rel), "사유": f"index_guard: {guard}"})
            continue
        if p.suffix.lower() in HWP_확장:
            conv = 변환본(p)
            if conv is None:
                skipped.append({**rec, "path": str(rel),
                                "사유": "HWP 변환본 없음 — 한컴 변환 필요"})
                continue
            rec["path"] = conv
            rec["원본"] = str(rel)
        targets.append(rec)

    if only:
        targets = [t for t in targets if t["layer"] == only]
    return targets, skipped


def 분해(path: Path) -> tuple[list[dict], str]:
    """extract 계약: ('articles', list) 또는 ('text', (str, offsets))."""
    kind, payload = extract(path)
    if kind == "articles":
        # XML 은 law.go.kr 구조를 그대로 따 이미 조 단위다. split_articles 를 태우면
        # 오히려 깨진다 (실측: 조 0개). 필드명만 맞춘다.
        arts = []
        for a in payload:
            arts.append({
                "조번호": a.get("조번호") or a.get("조문번호") or "",
                "조제목": a.get("조제목") or a.get("조문제목"),
                "조번호_int": a.get("조번호_int"),
                "본문": sanitize(a.get("본문") or a.get("조문내용") or ""),
                "페이지": None,
            })
        return arts, "xml_native"
    text, offsets = payload
    return split_articles(text, offsets)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["L1", "L2", "사례", "기타"])
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    t0 = time.time()
    targets, skipped = 대상수집(args.only)
    if args.limit:
        targets = targets[:args.limit]
    print(f"대상 {len(targets)}건 / 제외 {len(skipped)}건\n")

    articles: dict[str, dict] = {}
    report = {"성공": [], "게이트경고": [], "실패": [],
              "제외": [{**s, "path": str(s.get("path"))} for s in skipped]}
    strat_c, layer_c = Counter(), Counter()
    총조 = 0

    for i, t in enumerate(targets, 1):
        p: Path = t["path"]
        try:
            arts, strat = 분해(p)
        except Exception as e:                      # noqa: BLE001
            report["실패"].append({"doc_id": t["doc_id"], "layer": t["layer"],
                                   "path": str(p), "오류": f"{type(e).__name__}: {e}"[:200]})
            print(f"[{i:>3}/{len(targets)}] !! {t['doc_id'][:52]}  {type(e).__name__}")
            continue

        v = validate(arts, strat)
        rec = {"doc_id": t["doc_id"], "layer": t["layer"], "strategy": strat,
               "조": len(arts), "최장": max((len(a["본문"]) for a in arts), default=0),
               "quality": v["quality"], "flags": v["flags"]}
        articles[t["doc_id"]] = {**rec, "path": str(p.relative_to(ROOT)),
                                 "규범": t.get("규범"), "articles": arts}
        (report["게이트경고"] if v["flags"] else report["성공"]).append(rec)
        strat_c[strat] += 1
        layer_c[t["layer"]] += 1
        총조 += len(arts)
        if i % 25 == 0 or i == len(targets):
            print(f"[{i:>3}/{len(targets)}] 진행 — 누적 조 {총조:,}")

    el = time.time() - t0
    report["요약"] = {
        "생성": "scripts/stage0_run.py",
        "대상": len(targets), "성공": len(report["성공"]),
        "게이트경고": len(report["게이트경고"]), "실패": len(report["실패"]),
        "제외": len(skipped), "총_조": 총조,
        "전략": dict(strat_c), "레이어": dict(layer_c),
        "소요초": round(el, 1),
    }
    OUT_ART.write_text(json.dumps(articles, ensure_ascii=False), encoding="utf-8")
    OUT_REP.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print()
    print(f"성공 {len(report['성공'])} / 경고 {len(report['게이트경고'])} / "
          f"실패 {len(report['실패'])} / 제외 {len(skipped)}")
    print(f"총 조 {총조:,}  ·  {el:.1f}초")
    print("전략:", dict(strat_c))
    print("레이어:", dict(layer_c))
    print(f"\n-> {OUT_ART.relative_to(ROOT)}  ({OUT_ART.stat().st_size/1e6:.1f} MB)")
    print(f"-> {OUT_REP.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
