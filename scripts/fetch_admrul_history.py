# -*- coding: utf-8 -*-
"""행정규칙(고시·훈령·예규) 구판 수집.

법령은 `target=eflaw` + `efYd` 로 시점본을 잡지만 **행정규칙은 eflaw 대상이 아니다.**
대신 `lawSearch.do?target=admrul&nw=2` 가 현행+연혁을 모두 준다 (`nw=1` 은 현행만).
목록의 `행정규칙일련번호` 로 `lawService.do?target=admrul&ID=` 를 치면 그 판본 본문이 온다.

주의: 구판은 **제명이 다를 수 있다.** 실측 — 「창업 및 창업기업 범위에 관한 규정」(2022 제정)
→ 현행 「창업기업 및 국외 창업기업 범위에 관한 규정」. 그래서 검색은 현행 제명으로 하되
파일명은 각 판본 자신의 제명을 쓴다. 묶는 키는 개정 불변인 `행정규칙ID`.

실행:
    python scripts/fetch_admrul_history.py --dry-run
    python scripts/fetch_admrul_history.py [--since 2023]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import Law_Crawling as L  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "법령 PDF" / "L1_법령"
HIST = SRC / "연혁"
SEARCH = "http://www.law.go.kr/DRF/lawSearch.do"


def on_disk_admruls() -> list[dict]:
    """디스크의 행정규칙 XML → 현행 제명·ID·일련번호."""
    out = []
    for f in sorted(SRC.glob("*.xml")):
        root = ET.parse(f).getroot()
        if root.findall(".//조문단위"):
            continue                      # 법령
        info = root.find(".//행정규칙기본정보")
        if info is None:
            continue

        def g(t):
            e = info.find(t)
            return (e.text or "").strip() if e is not None and e.text else ""

        out.append({"file": f.name, "name": g("행정규칙명"), "id": g("행정규칙ID"),
                    "seq": g("행정규칙일련번호"), "eff": g("시행일자")})
    return out


def versions(name: str, adm_id: str) -> list[dict]:
    """nw=2 → 현행 + 연혁 전체. 같은 행정규칙ID 인 것만 남긴다."""
    r = requests.get(SEARCH, params={"OC": L.OC, "type": "XML", "target": "admrul",
                                     "query": name, "nw": "2", "display": "100"}, timeout=30)
    root = ET.fromstring(r.text)
    out = []
    for it in root.findall(".//admrul"):
        if (it.findtext("행정규칙ID") or "").strip() != adm_id:
            continue
        out.append({"seq": (it.findtext("행정규칙일련번호") or "").strip(),
                    "name": (it.findtext("행정규칙명") or "").strip(),
                    "eff": (it.findtext("시행일자") or "").strip(),
                    "kind": (it.findtext("제개정구분명") or "").strip(),
                    "cur": (it.findtext("현행연혁구분") or "").strip()})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--since", type=int, default=0, help="이 연도 이후 시행본만 (기본 전체)")
    ap.add_argument("--all", action="store_true",
                    help="판정 인덱스 제외분의 구판까지 받는다 (기본은 편입분만)")
    args = ap.parse_args()

    api = L.Api(L.OC)
    rules = on_disk_admruls()
    n_all = len(rules)
    # 판정 인덱스에서 뺀 규범(타부처 보조금 규정 등)은 구판도 받지 않는다.
    # 근거 사슬에는 현행본만 있으면 되고, 구판까지 받으면 노이즈만 수백 건 는다.
    src_p = ROOT / "법령 PDF" / "_law_sources.json"
    if not args.all and src_p.exists():
        src = json.loads(src_p.read_text(encoding="utf-8"))
        skip = {k for k, v in src.items() if not v.get("index", True)}
        rules = [r for r in rules if L.norm(r["name"]) not in skip]
    print("디스크 행정규칙 %d건 → 대상 %d건 (인덱스 제외 %d건 건너뜀)"
          % (n_all, len(rules), n_all - len(rules)))

    plan, log = [], []
    for r in rules:
        try:
            vs = versions(r["name"], r["id"])
        except Exception as e:                                   # noqa: BLE001
            print("  ! %-44s 목록 조회 실패 %s" % (r["name"][:44], e))
            continue
        time.sleep(L.SLEEP_SEC)
        old = [v for v in vs if v["cur"] != "현행" and v["seq"] != r["seq"]
               and (not args.since or int(v["eff"][:4] or 0) >= args.since)]
        renamed = {v["name"] for v in old if v["name"] != r["name"]}
        tag = "  (구제명: %s)" % ", ".join(sorted(renamed))[:46] if renamed else ""
        print("  %-46s 전체 %d판 / 구판 %d판%s" % (r["name"][:46], len(vs), len(old), tag))
        for v in old:
            plan.append((r, v))

    print("\n내려받을 구판 %d개" % len(plan))
    if args.dry_run:
        return

    HIST.mkdir(parents=True, exist_ok=True)
    ok = fail = skip = 0
    for i, (r, v) in enumerate(plan, 1):
        dest = HIST / ("L1_%s_%s_%s.xml" % (L.safe_name(v["name"]), v["eff"], v["seq"]))
        if dest.exists():
            skip += 1
            continue
        try:
            body = api.body_xml("admrul", ID=v["seq"])
            root = ET.fromstring(body)                    # 파싱되는지 확인 후 저장
            n = len([c for c in root if c.tag == "조문내용"])
            if n == 0:
                raise RuntimeError("조문내용 0개")
            L.save_xml(dest, body)
            ok += 1
            print("[%2d/%d] OK  조문%3d  %s" % (i, len(plan), n, dest.name))
            log.append({"adm_id": r["id"], "current_name": r["name"], "version_name": v["name"],
                        "seq": v["seq"], "eff": v["eff"], "kind": v["kind"],
                        "file": "법령 PDF/L1_법령/연혁/" + dest.name, "status": "ok"})
        except Exception as e:                                    # noqa: BLE001
            fail += 1
            print("[%2d/%d] FAIL %s <- %s" % (i, len(plan), v["seq"], e))
            log.append({"adm_id": r["id"], "current_name": r["name"], "seq": v["seq"],
                        "eff": v["eff"], "status": "fail", "error": str(e)})
        time.sleep(L.SLEEP_SEC)

    (ROOT / "법령 PDF" / "_admrul_history_log.json").write_text(
        json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n성공 %d / 실패 %d / 이미 있음 %d" % (ok, fail, skip))


if __name__ == "__main__":
    main()
