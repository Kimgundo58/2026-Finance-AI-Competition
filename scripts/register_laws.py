# -*- coding: utf-8 -*-
"""Law_Crawling.py 수집 결과를 manifest.json 에 등록한다.

정책 (manifest.json status_values / 파이프라인 §2.1 을 따른다)
  현행본  status=active,     role=[judgment_index], index=true   → chunks 까지
  시점본  status=superseded, role=[diff_only],      index=false  → doc_articles 만
          ("개정으로 대체된 구버전 — diff 데모 전용, 판정 인덱스 금지")

실행:
    python scripts/register_laws.py --dry-run    변경 내역만 출력
    python scripts/register_laws.py              manifest.json 갱신 (.bak 자동 생성)
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "manifest.json"
REPORT = ROOT / "법령 PDF" / "_law_report.json"


def fmt_version(eff: str) -> str:
    """20260701 → '시행 2026-07-01' (기존 L1 항목 표기)."""
    if eff and len(eff) == 8 and eff.isdigit():
        return f"시행 {eff[:4]}-{eff[4:6]}-{eff[6:]}"
    return eff or ""


def build_entries(results: list[dict]) -> list[dict]:
    entries = []
    for r in results:
        if r.get("status") != "수집":
            continue
        cited = r.get("cited") or []
        cited_s = ", ".join(f"제{c}조" if not c.startswith("별표") else c for c in cited[:8])
        if len(cited) > 8:
            cited_s += f" 외 {len(cited) - 8}건"

        for f in r["files"]:
            is_cur = f["kind"] == "현행"
            note = [f"{r['ref_id']}", f"법령ID {r.get('law_id')}", f"MST {f['mst']}"]
            if r.get("promulgation_no"):
                note.append(f"공포 {r['promulgation_no']}")
            if is_cur and cited_s:
                note.append(f"인용 {cited_s}")
            if not is_cur:
                note.append(f"{f.get('doc_year')}년 문서 대조용")
            for fl in r.get("flags", []):
                note.append(fl)

            entries.append({
                "file": f["file"],
                "layer": "L1",
                "doc_type": r.get("law_type") or r.get("doc_type") or "법률",
                "version": fmt_version(f["effective_date"]),
                "status": "active" if is_cur else "superseded",
                "role": ["judgment_index"] if is_cur else ["diff_only"],
                "index": bool(is_cur),
                "notes": " · ".join(str(x) for x in note if x),
            })
    return entries


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not REPORT.exists():
        print("수집 리포트가 없습니다. 먼저 `python Law_Crawling.py` 를 실행하세요.")
        sys.exit(1)

    results = json.loads(REPORT.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    files = manifest["files"]
    by_path = {f["file"]: i for i, f in enumerate(files)}

    new_entries = build_entries(results)
    added, replaced = [], []
    for e in new_entries:
        if e["file"] in by_path:
            old = files[by_path[e["file"]]]
            # 기존 항목의 사람이 쓴 notes 와 role 은 보존한다.
            merged = dict(e)
            if old.get("notes") and old["notes"] not in e["notes"]:
                merged["notes"] = f"{old['notes']} · {e['notes']}"
            if old.get("status") == "reference":
                merged.update({"status": "reference", "role": old.get("role", ["archive"]),
                               "index": False})
            files[by_path[e["file"]]] = merged
            replaced.append(e["file"])
        else:
            files.append(e)
            added.append(e["file"])

    cur = sum(1 for e in new_entries if e["status"] == "active")
    hist = len(new_entries) - cur
    print(f"등록 대상 {len(new_entries)}건 (현행 {cur} → index=true / 시점본 {hist} → index=false)")
    print(f"  신규 추가 {len(added)} · 기존 갱신 {len(replaced)}")
    if replaced:
        print("\n기존 항목 갱신:")
        for p in replaced:
            print(f"  {p}")

    # 미수집 항목을 missing 에 남긴다
    manual = [r for r in results if r.get("status") in ("수동수집", "0건")]
    if manual:
        print(f"\n수동 확보 필요 {len(manual)}건 → manifest.missing 에 기록")
        for r in manual:
            print(f"  {r['ref_id']} {r['keyword']}")
    existing_missing = {m.get("doc") for m in manifest.get("missing", [])}
    for r in manual:
        if r["keyword"] in existing_missing:
            continue
        manifest.setdefault("missing", []).append({
            "doc": r["keyword"],
            "layer": "L2" if r["ref_id"] in ("R05", "R06") else "L1",
            "priority": 1 if r["ref_id"] in ("R05", "R06") else 3,
            "how": r.get("reason", ""),
            "why": f"{r['ref_id']} — 세부관리기준이 인용하는 규범. law.go.kr 미등재",
        })

    if args.dry_run:
        print("\n--dry-run: manifest.json 은 변경하지 않았습니다.")
        return

    shutil.copy2(MANIFEST, MANIFEST.with_suffix(".json.bak"))
    manifest["files"] = files
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nmanifest.json 갱신 완료 (백업: {MANIFEST.with_suffix('.json.bak').name})")
    print(f"  총 문서 {len(files)}건 / L1 {sum(1 for f in files if f.get('layer') == 'L1')}건")


if __name__ == "__main__":
    main()
