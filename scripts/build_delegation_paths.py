# -*- coding: utf-8 -*-
"""위임 계통(delegation path) 복원.

`Law_Crawling.py` 는 위임을 따라가며 하위법령을 내려받지만, 그때 만든
`delegated_from` / `delegate_via` / `depth` 가 `_law_report.json` 에 병합되지
않아 **어느 법 몇 조에서 위임됐는지가 유실**됐다. 답변 근거 제시에 필요한
정보이므로 같은 BFS 를 다시 돌려 간선만 복원한다 (본문 재다운로드 없음).

산출: `법령 PDF/_law_delegation_paths.json`
    { 규범명: {chain: [...], parents: [...], depth: n, file: ...} }

실행:
    python scripts/build_delegation_paths.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))
import Law_Crawling as L  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "법령 PDF" / "L1_법령"
REPORT = ROOT / "법령 PDF" / "_law_report.json"
OUT = ROOT / "법령 PDF" / "_law_delegation_paths.json"
CACHE_P = ROOT / "법령 PDF" / "_deleg_resolve_cache.json"

MAX_DEPTH = 2   # Law_Crawling.crawl_delegated 와 동일


def disk_index() -> dict[str, str]:
    """정규화 규범명 → 파일명."""
    idx = {}
    for f in sorted(SRC.glob("*.xml")):
        root = ET.parse(f).getroot()
        for tag in ("법령명_한글", "행정규칙명"):
            e = root.find(".//" + tag)
            if e is not None and e.text:
                idx.setdefault(L.norm(e.text.strip()), f.name)
    return idx


def main() -> None:
    rep = json.loads(REPORT.read_text(encoding="utf-8"))
    api = L.Api()
    have = disk_index()
    cache = json.loads(CACHE_P.read_text(encoding="utf-8")) if CACHE_P.exists() else {}

    def resolve(title: str, is_adm: bool) -> str | None:
        """제명 → 현행 규범명. lsDelegated 가 주는 제명은 구판이라 재조회가 필요하다."""
        key = ("adm:" if is_adm else "law:") + title
        if key in cache:
            return cache[key]
        hit = L.resolve_admrul(api, title) if is_adm else L.resolve_law(api, title)
        time.sleep(L.SLEEP_SEC)
        nm = (hit or {}).get("행정규칙명" if is_adm else "법령명한글")
        cache[key] = nm.strip() if nm else None
        return cache[key]

    seeds = [x for x in rep if x.get("status") == "수집"
             and x.get("target") == "law" and x.get("mst")]
    cited_by = {L.norm(x["name"]): set(x.get("cited") or []) for x in rep if x.get("name")}

    # 간선: child_norm -> [{parent, kind, via, depth}]
    edges: dict[str, list[dict]] = {}
    queue = [(x["name"], x["mst"], 0) for x in seeds]
    seen_mst: set[str] = set()

    while queue:
        name, mst, depth = queue.pop(0)
        if depth >= MAX_DEPTH or mst in seen_mst:
            continue
        seen_mst.add(mst)

        cited = cited_by.get(L.norm(name), set())
        keep, _ = L.delegated_targets(api, mst, cited, parent=name)
        time.sleep(L.SLEEP_SEC)
        print("  [%s] 위임 %d건" % (name[:34], len(keep)))

        for kind, title, jo in keep:
            is_adm = kind == "위임행정규칙"
            cur = resolve(title, is_adm)
            if not cur:
                print("      · %s %s → 현행 조회 실패" % (kind, title[:34]))
                continue
            ck = L.norm(cur)
            rec = {"parent": name, "kind": kind, "via": jo, "depth": depth + 1}
            if cur != title:
                rec["구판제명"] = title
            if rec not in edges.setdefault(ck, []):
                edges[ck].append(rec)

            if not is_adm and ck in have:
                # 시행령·시행규칙은 인용 조를 물려받아 다음 단계로
                cited_by.setdefault(ck, cited)
                hit = L.resolve_law(api, cur)
                time.sleep(L.SLEEP_SEC)
                if hit and hit.get("법령일련번호"):
                    queue.append((cur, hit["법령일련번호"], depth + 1))

    CACHE_P.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")

    # 시드까지 거슬러 올라가 사슬을 만든다
    seed_norms = {L.norm(x["name"]) for x in rep if x.get("name")}

    def chain_of(node: str, guard: tuple[str, ...] = ()) -> list[str]:
        if node in seed_norms or node not in edges or node in guard:
            return [node]
        p = min(edges[node], key=lambda e: e["depth"])
        return chain_of(L.norm(p["parent"]), guard + (node,)) + [node]

    out = {}
    for ck, parents in edges.items():
        p0 = min(parents, key=lambda e: e["depth"])
        out[ck] = {
            "file": have.get(ck),
            "on_disk": ck in have,
            "depth": p0["depth"],
            "kind": p0["kind"],
            "delegated_from": p0["parent"],
            "delegate_via": p0["via"],
            "chain": chain_of(ck),
            "all_parents": parents,
        }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    ondisk = sum(1 for v in out.values() if v["on_disk"])
    print("\n간선 복원 %d개 규범 (디스크 보유 %d) → %s" % (len(out), ondisk, OUT.name))


if __name__ == "__main__":
    main()
