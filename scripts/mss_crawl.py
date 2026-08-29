# -*- coding: utf-8 -*-
"""중기부(L1) 참조 법령 수집기.

입력은 `법령 PDF/_mss_master.json` (중기부 문서 84건 마스터).
창진원 배치와 **같은 폴더에 저장**하되, 출처는 `_law_sources.json` 으로 구분한다.
같은 규범을 두 배치가 함께 참조하는 경우가 27건 있어 파일을 두 벌 두면 안 된다.

단계:
  1. 현행본 수집 (법령=target law / 행정규칙=target admrul)
  2. 법령 시점본 (DOC_YEARS)
  3. 위임 추적 3단계 (Law_Crawling.crawl_delegated 재사용)

행정규칙 구판과 별표 첨부는 별도 스크립트가 디스크 전체를 훑으므로 이후 실행한다:
  scripts/fetch_admrul_history.py · scripts/fetch_admrul_attachments.py

실행:
    python scripts/mss_crawl.py --dry-run
    python scripts/mss_crawl.py [--no-history] [--no-delegated]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))
import Law_Crawling as L  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MASTER = ROOT / "법령 PDF" / "_mss_master.json"
REPORT = ROOT / "법령 PDF" / "_mss_report.json"
DROPPED = ROOT / "법령 PDF" / "_mss_delegated_dropped.json"
CACHE_P = ROOT / "법령 PDF" / "_law_cache.json"
SOURCE = "중기부"


def disk_index() -> dict[str, str]:
    idx = {}
    for f in sorted(L.OUT_DIR.glob("*.xml")):
        root = ET.parse(f).getroot()
        for tag in ("법령명_한글", "행정규칙명"):
            e = root.find(".//" + tag)
            if e is not None and e.text:
                idx.setdefault(L.norm(e.text.strip()), f.name)
    return idx


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-history", action="store_true")
    ap.add_argument("--no-delegated", action="store_true")
    args = ap.parse_args()

    master = json.loads(MASTER.read_text(encoding="utf-8"))
    api = L.Api(L.OC)
    have = disk_index()
    cache = json.loads(CACHE_P.read_text(encoding="utf-8")) if CACHE_P.exists() else {}
    L.HIST_DIR.mkdir(parents=True, exist_ok=True)

    print("마스터 %d건 / 디스크 보유 규범 %d개\n" % (len(master), len(have)))
    results = []

    for it in master:
        nm, tgt = it["name"], it["target"]
        rec = {"ref_id": "M%02d" % it["no"], "keyword": nm, "target": tgt,
               "doc_type": it["doc_type"], "cited": it["cited"], "docs": it["docs"],
               "note": it["note"], "source": SOURCE, "files": [], "flags": [],
               "article_titles": [], "status": "미확인"}

        hit = (L.resolve_law(api, nm) if tgt == "law" else L.resolve_admrul(api, nm))
        time.sleep(L.SLEEP_SEC)
        if not hit:
            rec["status"] = "미등재"
            rec["flags"].append("법령정보센터 미등재")
            print("  ✗ M%-3d %-46s 미등재" % (it["no"], nm[:46]))
            results.append(rec)
            continue

        if tgt == "law":
            name, eff = hit.get("법령명한글"), hit.get("시행일자")
            lid, ser = hit.get("법령ID"), hit.get("법령일련번호")
            ltype, pno = hit.get("법령구분명"), hit.get("공포번호")
        else:
            name, eff = hit.get("행정규칙명"), hit.get("시행일자")
            lid, ser = hit.get("행정규칙ID"), hit.get("행정규칙일련번호")
            ltype, pno = hit.get("행정규칙종류"), hit.get("발령번호")

        rec.update(name=name, law_id=lid, mst=ser, effective_date=eff,
                   law_type=ltype, promulgation_no=pno,
                   ministry=hit.get("소관부처명"))
        if L.norm(name) != L.norm(nm):
            rec["flags"].append("제명차이(%s→%s)" % (nm, name))

        fname = "L1_%s_%s.xml" % (L.safe_name(name), eff)
        fpath = L.OUT_DIR / fname
        already = L.norm(name) in have
        rec["dedup"] = "기보유(창진원 배치와 공유)" if already else "신규"

        if args.dry_run:
            print("  · M%-3d %-44s %s 시행 %s" % (it["no"], name[:44], rec["dedup"], eff))
            rec["status"] = "확인"
            results.append(rec)
            continue

        if not fpath.exists():
            body = (api.body_xml("law", MST=ser) if tgt == "law"
                    else api.body_xml("admrul", ID=ser))
            L.save_xml(fpath, body)
            time.sleep(L.SLEEP_SEC)
        else:
            body = fpath.read_text(encoding="utf-8")

        rec["files"].append({"file": "법령 PDF/L1_법령/" + fname, "kind": "현행",
                             "mst": ser, "effective_date": eff})
        rec["status"] = "수집"
        have[L.norm(name)] = fname

        if tgt == "law" and it["cited"]:
            rec["article_titles"] = L.article_titles(body, it["cited"])
            if any(not t["ok"] for t in rec["article_titles"]):
                rec["flags"].append("조문없음")
        print("  → M%-3d %-44s %s 시행 %s" % (it["no"], name[:44], rec["dedup"], eff))

        # 시점본 — 법령만. 행정규칙은 eflaw 대상이 아니라 별도 스크립트(nw=2)가 처리한다.
        if not args.no_history and tgt == "law":
            for h in L.historical_msts(api, name):
                hm = h["법령일련번호"]
                if hm == ser:
                    continue
                heff = h.get("시행일자")
                hname = "L1_%s_%s_%s.xml" % (L.safe_name(name), heff, hm)
                hpath = L.HIST_DIR / hname
                if not (cache.get("hist:%s" % hm) and hpath.exists()):
                    L.save_xml(hpath, api.body_xml("law", MST=hm))
                    cache["hist:%s" % hm] = heff
                    time.sleep(L.SLEEP_SEC)
                rec["files"].append({"file": "법령 PDF/L1_법령/연혁/" + hname,
                                     "kind": "시점본", "mst": hm,
                                     "effective_date": heff, "doc_year": h.get("_year")})
            n_h = sum(1 for f in rec["files"] if f["kind"] == "시점본")
            if n_h:
                print("        연혁 %d건" % n_h)
        results.append(rec)

    CACHE_P.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")

    dropped = []
    if not args.dry_run and not args.no_delegated:
        print("\n── 위임 추적 3단계 ──")
        collected = [r for r in results if r["status"] == "수집"]
        extra, dropped = L.crawl_delegated(api, collected, max_depth=2)
        for e in extra:
            e["source"] = SOURCE
        results.extend(extra)
        print("위임으로 추가 수집 %d건 / 범위 밖 제외 %d건" % (len(extra), len(dropped)))

    if not args.dry_run:
        REPORT.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        DROPPED.write_text(json.dumps(dropped, ensure_ascii=False, indent=1), encoding="utf-8")

    from collections import Counter
    print("\n상태:", dict(Counter(r["status"] for r in results)))
    print("중복(창진원 공유):", sum(1 for r in results if r.get("dedup", "").startswith("기보유")))


if __name__ == "__main__":
    main()
