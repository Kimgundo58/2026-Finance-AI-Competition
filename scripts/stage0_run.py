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
import re
import sys
import time
from collections import Counter
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import pdftext                                    # noqa: E402  문자중복/다단 해소
from index_guard import reject_reason            # noqa: E402
from stage0_extract import extract               # noqa: E402
from stage0_articles import split_articles, validate, sanitize, is_deleted, _clean  # noqa: E402

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


RE_조번호_INT = re.compile(r"제\s*(\d+)\s*조")


RE_PARA = re.compile(r"\n{2,}")

# 사례 단위 상한. Stage 2 청킹 임계(3,000자)와 맞춘다.
사례_최대 = 3000
사례_최소 = 100


def _사례_분할(text: str) -> list[str]:
    """사례 문서를 다루기 좋은 크기로 자른다.

    🔴 빈 줄만으로 자르면 안 된다. PDF 텍스트에는 빈 줄이 거의 없어서
    실측(권익위재결례집)에서 **한 단락이 222,683자**가 나왔다 — 문서 하나가 통째로
    단락 하나다. 그 상태로는 Stage 0 산출물이 아무 쓸모가 없다.

    빈 줄 분할을 먼저 시도하고, 상한을 넘는 덩어리는 **줄 단위로 다시 묶어** 자른다.
    사례는 판정 근거가 아니라 B등급 참고 자료이고 최종 청킹 단위(Q&A)는 Stage 2 가
    정하므로, 여기서는 "덩어리가 과하게 크지 않을 것" 까지만 보장한다.
    """
    out: list[str] = []
    for block in RE_PARA.split(text):
        block = block.strip()
        if len(block) < 사례_최소:
            continue
        if len(block) <= 사례_최대:
            out.append(block)
            continue
        buf: list[str] = []
        size = 0
        for line in block.split("\n"):
            if size + len(line) > 사례_최대 and buf:
                out.append("\n".join(buf))
                buf, size = [], 0
            buf.append(line)
            size += len(line) + 1
        if buf:
            tail = "\n".join(buf).strip()
            if len(tail) >= 사례_최소:
                out.append(tail)
            elif out:
                out[-1] += "\n" + tail
    return out


def 분해(path: Path, layer: str | None = None) -> tuple[list[dict], str]:
    """XML 은 law.go.kr 구조 그대로, PDF 는 pdftext -> split_articles.

    🔴 **사례 레이어에는 조 분해를 태우지 않는다** (2026-08-30).
    사례집·재결례집·판례는 조 체계 문서가 아니라 Q&A / 사건 단위다. 그런데
    `split_articles` 를 태우면 **본문에 인용된 타 법령 조문을 그 문서의 조로 잡는다.**
    실측:
        사례집_권익위재결례집_1        첫 조 = `제382조의3(이사의 충실의무)`  <- 상법 조문
        사례집_KISTEP_판례조사분석      첫 조 = `제35조(연구개발과제의 성실 수행)` <- 혁신법 조문
        사례집_한국연구재단_QA사례집    첫 조 = `제73조(사전 승인 대상)`        <- 인용 규정
    셋 다 `quality=high` 로 나와서, 그대로 두면 **남의 법 조문이 사례집 doc_id 를 달고
    조 단위로 인덱스에 들어간다.** 인용하면 출처가 틀린 답이 된다.

    사례는 `case_chunks`(B등급) 로 가고 청킹 단위는 Q&A 다 (`RAG.md` §1·§2-2).
    Stage 0 에서는 단락 분할까지만 하고 `parse_quality='low'` 로 두어
    판정 인덱스에서 자동으로 빠지게 한다.
    """
    if layer == "사례":
        if path.suffix.lower() == ".pdf":
            text, _ = pdftext.extract_meta(path)
        else:
            _, payload = extract(path)
            text = payload[0] if isinstance(payload, tuple) else str(payload)
        paras = _사례_분할(_clean(text))
        return ([{"조번호": f"단락{i+1:03d}", "조제목": None, "조번호_int": None,
                  "본문": sanitize(p), "페이지": None} for i, p in enumerate(paras)],
                "case_paragraph")

    if path.suffix.lower() == ".xml":
        # XML 은 이미 조 단위다. split_articles 를 태우면 오히려 깨진다 (실측: 조 0개).
        _, payload = extract(path)
        arts = []
        for a in payload:
            조번호 = a.get("조번호") or a.get("조문번호") or ""
            n = a.get("조번호_int")
            if n is None:
                # 🔴 `extract_xml()` 은 이 키를 주지 않는다. 그대로 두면 법령 219건
                #    22,551조 전부 None 이 되어 V1 단조성 검증이 무력화되고
                #    `ix_articles_doc (doc_id, 조번호_int)` 정렬도 의미를 잃는다.
                m = RE_조번호_INT.search(조번호)
                n = int(m.group(1)) if m else None
            arts.append({
                "조번호": 조번호,
                "조제목": a.get("조제목") or a.get("조문제목"),
                "조번호_int": n,
                "본문": sanitize(a.get("본문") or a.get("조문내용") or ""),
                "페이지": None,
            })
        return arts, "xml_native"

    # 🔴 PDF 는 반드시 `pdftext.extract_meta()` 를 탄다.
    #    `stage0_extract.extract_pdf()` 는 문자중복 레이어를 해소하지 않는다.
    #    실측 사고: 통합관리지침 제14차가 `"창창업업기기업업등등"` 상태로 들어와
    #    `제\d+조` 가 하나도 안 걸리고 `outline_numbered` 로 떨어졌다 — 조 86개가 30개가 됐다.
    #    이 문서는 판정 최상위 근거다 (CLAUDE.md 파싱 함정).
    #    2단·4분면 조판 해소도 이 경로에만 있다.
    if path.suffix.lower() == ".pdf":
        text, meta = pdftext.extract_meta(path)
        return split_articles(text, meta.get("page_offsets") or {})

    _, (text, offsets) = extract(path)
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
            arts, strat = 분해(p, layer=t["layer"])
        except Exception as e:                      # noqa: BLE001
            report["실패"].append({"doc_id": t["doc_id"], "layer": t["layer"],
                                   "path": str(p), "오류": f"{type(e).__name__}: {e}"[:200]})
            print(f"[{i:>3}/{len(targets)}] !! {t['doc_id'][:52]}  {type(e).__name__}")
            continue

        # 폐지 조문을 표시해 둔다. Stage 2 가 인덱스에서 뺀다 —
        # 효력 없는 조를 근거로 인용하면 오답이다.
        n_del = 0
        for a in arts:
            if is_deleted(a):
                a["삭제"] = True
                n_del += 1

        v = validate(arts, strat)
        # `참고:` 접두 플래그는 사실 기록이지 경고가 아니다. 경고 집계에서 뺀다 —
        # 섞으면 진짜 파싱 실패가 소음에 묻힌다.
        경고 = [f for f in v["flags"] if not f.startswith("참고:")]
        rec = {"doc_id": t["doc_id"], "layer": t["layer"], "strategy": strat,
               "조": len(arts), "삭제조": n_del,
               "최장": max((len(a["본문"]) for a in arts), default=0),
               "quality": v["quality"], "flags": v["flags"]}
        articles[t["doc_id"]] = {**rec, "path": str(p.relative_to(ROOT)),
                                 "규범": t.get("규범"), "articles": arts}
        (report["게이트경고"] if 경고 else report["성공"]).append(rec)
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
