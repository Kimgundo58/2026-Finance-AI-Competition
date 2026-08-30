# -*- coding: utf-8 -*-
"""누락 규범 지정 수집 — `_missing_norms.json` -> `법령 PDF/L1_법령/`.

`Law_Crawling.py` 는 마스터 md(`중기부_법령_링크모음.md`)에 적힌 목록만 훑는다.
이 스크립트는 **참조 그래프에서 도출된 누락분**을 이름으로 지정해 받는다.

배경 (`법령_크롤링_현황.md` §9 #1)
    L1 수집 범위가 폐기된 구 PDF 기준이라, 현행 세부관리기준이 인용하는 규범 중
    코퍼스에 없는 것이 있었다. `_refs.json` 을 전 코퍼스로 확장한 뒤 전수 추출해
    13종을 확정했다 — 고용보험법(인건비 4대보험 예외) 등 판정 직결분 7종 포함.

해소기(`Law_Crawling.resolve_law` / `resolve_admrul`)를 그대로 쓴다.
검색이 매우 느슨해서(query=상법 -> 56건) 완전일치 필터 없이는 엉뚱한 법을 집는다.

실행:
    $env:LAW_GO_KR_OC = "<신청ID>"
    python scripts/fetch_missing_norms.py            # 수집
    python scripts/fetch_missing_norms.py --dry-run  # 해결만 하고 저장 안 함
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import Law_Crawling as LC  # noqa: E402

SRC = ROOT / "법령 PDF" / "_missing_norms.json"
OUT = ROOT / "법령 PDF" / "_missing_norms_report.json"

# 정식 제명. 세부관리기준의 인용 표기가 법령의 현행 제명과 다른 경우가 있다.
# 2026-08-30 검색으로 확인한 실제 제명이다 — 추정이 아니라 실측이다.
정식제명 = {
    # 인용 표기                              -> law.go.kr 현행 제명
    "근로자직업능력개발법": "국민 평생 직업능력 개발법",
    #   ^ 2022 전부개정으로 제명이 바뀌었다. 세부관리기준이 구 제명으로 인용한다
    "대·중소기업 상생협력 촉진에 관한 법률": "대ㆍ중소기업 상생협력 촉진에 관한 법률",
    #   ^ 가운뎃점이 U+00B7(·) 이 아니라 U+318D(ㆍ) 다. 눈으로는 같아 보인다
    "중소기업기술개발 지원사업 관리지침": "중소기업기술개발 지원사업 운영요령",
    #   ^ '관리지침' 이라는 규범은 없다. TIPS 총괄 운영지침이 위임받는 상위는 운영요령이다
}

# law.go.kr 에 없는 것 — 수집 불가. 사유를 남긴다.
미등재 = {
    "신사업창업사관학교 운영지침":
        "검색 0건. 중기부 내부 지침으로 국가법령정보센터 미등재. "
        "모두의창업 로컬트랙이 준용하므로 중기부 배포본을 따로 구해야 한다",
    "창업지원사업 제3자 부당개입 근절을 위한 업무처리 지침":
        "검색 0건(공정위 '부당한 공동행위 심사지침' 은 별개 규범). 중기부 내부 지침",
    "예산 및 기금운용계획 집행지침":
        "기재부가 매년 발간하는 지침. 법령이 아니라 예산 문서라 API 대상이 아니다",
}

# 종류 -> law.go.kr target
TARGET = {"법률": "law", "시행령": "law", "시행규칙": "law", "행정규칙": "admrul"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--priority", default="", help="높음 / 중간 / 낮음 만 (쉼표 구분)")
    args = ap.parse_args()

    LC.require_oc()
    api = LC.Api()
    spec = json.loads(SRC.read_text(encoding="utf-8"))
    items = spec["수집_대상"]
    if args.priority:
        want = {x.strip() for x in args.priority.split(",")}
        items = [x for x in items if x["우선순위"] in want]
    print(f"수집 대상 {len(items)}종"
          f" ({'해결만' if args.dry_run else '저장'})\n")

    results = []
    for i, it in enumerate(items, 1):
        name, kind = it["규범명"], it["종류"]
        target = TARGET.get(kind, "law")
        정식 = 정식제명.get(name)
        rec = {"규범명": name, "종류": kind, "우선순위": it["우선순위"],
               "판정_접점": it["판정_접점"], "target": target,
               "상태": "미해결", "file": None, "flags": []}
        if name in 미등재:
            rec.update(상태="미등재", flags=[미등재[name]])
            results.append(rec)
            print(f"[{i:>2}/{len(items)}] 미등재 {name[:40]:<42} {미등재[name][:44]}")
            continue
        if 정식:
            rec["flags"].append(f"정식 제명: {정식}")
        try:
            질의 = 정식 or name
            hit = (LC.resolve_law(api, 질의, name) if target == "law"
                   else LC.resolve_admrul(api, 질의))
        except Exception as e:                                  # noqa: BLE001
            rec.update(상태="오류", flags=[f"{type(e).__name__}: {e}"[:120]])
            results.append(rec)
            print(f"[{i:>2}/{len(items)}] !! {name[:40]:<42} {type(e).__name__}")
            continue

        if not hit:
            results.append(rec)
            print(f"[{i:>2}/{len(items)}] -- {name[:40]:<42} 미해결")
            continue

        if target == "law":
             제명, eff, key = hit.get("법령명한글"), hit.get("시행일자"), {"MST": hit["법령일련번호"]}
        else:
            제명, eff, key = hit.get("행정규칙명"), hit.get("시행일자"), {"ID": hit["행정규칙일련번호"]}
        rec.update(제명=제명, 시행일자=eff, mst=list(key.values())[0], 상태="해결")
        if LC.norm(제명) != LC.norm(name) and not 정식:
            rec["flags"].append(f"제명 다름: {제명}")

        if not args.dry_run:
            body = api.body_xml(target, **key)
            # 0건 != 없음. OC 오타·미승인이면 HTTP 200 에 빈 결과가 온다 (§12 함정)
            if not body.lstrip().startswith("<") or len(body) < 500:
                rec.update(상태="본문없음", flags=rec["flags"] + [f"len={len(body)}"])
                results.append(rec)
                print(f"[{i:>2}/{len(items)}] !! {name[:40]:<42} 본문 비어 있음")
                continue
            fname = f"L1_{LC.safe_name(제명)}_{eff}.xml"
            LC.save_xml(LC.OUT_DIR / fname, body)
            rec.update(상태="수집", file=f"법령 PDF/L1_법령/{fname}", bytes=len(body))

        results.append(rec)
        mark = rec["상태"]
        print(f"[{i:>2}/{len(items)}] {mark:<5} {name[:40]:<42} {제명 or ''}"
              f" ({eff}){'  ' + ' / '.join(rec['flags']) if rec['flags'] else ''}")

    from collections import Counter
    c = Counter(r["상태"] for r in results)
    doc = {"생성": "scripts/fetch_missing_norms.py", "원본": str(SRC.relative_to(ROOT)),
           "요약": dict(c), "results": results}
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{dict(c)}")
    print(f"-> {OUT.relative_to(ROOT)}")
    미해결 = [r["규범명"] for r in results if r["상태"] not in ("수집", "해결")]
    if 미해결:
        print("\n미해결 — 제명이 다르거나 law.go.kr 미등재일 수 있다:")
        for n in 미해결:
            print(f"   {n}")


if __name__ == "__main__":
    main()
