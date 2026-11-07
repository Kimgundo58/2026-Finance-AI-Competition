#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""추출 묶음의 입력 폴더를 만든다 (docs/5_룰/5-7b_추출_에이전트_지침.md "입력 묶음").

입력: <묶음 폴더>/입력_명세.json, ontology/schema.json, corpus.doc_articles(읽기 전용).
출력: 입력_원문.md(조 · 항 단위, 조문 ID 머리말), 입력_문서키.json, 입력_기존노드.json.
PDF 복원본이 있으면(ontology/추출/복원/<문서키>.json 의 "덮어쓰기") 그 조의 블록을 복원본으로 쓰고 [원문결함] 을 표시한다.

명세 예: {"사업키": "모두의창업", "문서": [{"문서키": "모두의창업", "조": ["제33조", "제36조", "별지2"]},
                                           {"문서키": "안내책자", "조접두": ["p9-", "p10-"]}]}
추출자에게 corpus.rules · item_alias · 정답셋을 주지 않는다. 이 스크립트도 doc_articles 만 읽는다.

실행: PYTHONIOENCODING=utf-8 python scripts/ontology_input.py ontology/추출/모두의_창업/시범
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import db  # noqa: E402
from ontology_norm import 별지_행  # noqa: E402

저장소 = Path(__file__).resolve().parent.parent
_항기호 = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
_RE_항 = re.compile(f"[{_항기호}]")
_RE_페이지 = re.compile(r"(?m)^\s*-\s*\d{1,3}\s*-\s*$\n?")
_RE_호 = re.compile(r"(?m)^(\d{1,2})\.\s")
_RE_조 = re.compile(r"제\d+조")


def _항_나누기(본문: str) -> list[tuple[str | None, str]]:
    """조 본문을 (항 기호, 조각) 으로 나눈다. 항 앞부분(조 표제)은 첫 항에 붙인다. 항이 없으면 조 전체."""
    자리 = [m.start() for m in _RE_항.finditer(본문)]
    if not 자리:
        return [(None, 본문.strip())]
    out: list[tuple[str | None, str]] = []
    앞 = 본문[:자리[0]]
    for k, st in enumerate(자리):
        끝 = 자리[k + 1] if k + 1 < len(자리) else len(본문)
        조각 = 본문[st:끝].strip()
        out.append((조각[0], (앞 + 조각 if k == 0 else 조각).strip()))
    return out


def _호_나누기(조각: str) -> list[tuple[str | None, str]]:
    """정의 조문처럼 호가 곧 단위인 조를 호로 나눈다. 호 앞 문장은 호 없는 블록으로 남긴다."""
    자리 = list(_RE_호.finditer(조각))
    if not 자리:
        return [(None, 조각)]
    out: list[tuple[str | None, str]] = []
    if 조각[:자리[0].start()].strip():
        out.append((None, 조각[:자리[0].start()].strip()))
    for k, m in enumerate(자리):
        끝 = 자리[k + 1].start() if k + 1 < len(자리) else len(조각)
        out.append((f"{m.group(1)}호", 조각[m.start():끝].strip()))
    return out


def 블록_만들기(문서키: str, 조번호: str, 조제목: str | None, 본문: str, *, 호분할: bool = False,
              덮어쓰기: dict | None = None) -> list[dict]:
    """조 하나 → [{id, 머리, 본문, 원문결함}]. 표는 복원본(덮어쓰기)이 있으면 그것을 쓴다."""
    머리 = f"{조번호}({조제목})" if 조제목 else 조번호
    if 덮어쓰기 and 조번호 in 덮어쓰기:
        return [{"id": f"조:{문서키}#{조번호}{b.get('항호') or ''}", "머리": 머리, "본문": b["본문"],
                 "원문결함": bool(b.get("원문결함"))} for b in 덮어쓰기[조번호]]
    if 조번호.startswith("별지") and "|" in 본문:
        행 = 별지_행(본문)
        if 행:
            return [{"id": f"조:{문서키}#{조번호}/{k}", "머리": 머리, "본문": v, "원문결함": False} for k, v in 행]
    if not _RE_조.match(조번호):                # 책자 · 붙임 · 편 단위 문서처럼 항 번호가 조문 번호가 아닌 문서
        return [{"id": f"조:{문서키}#{조번호}", "머리": 머리, "본문": 본문.strip(), "원문결함": False}]
    out = []
    for 항, 조각 in _항_나누기(_RE_페이지.sub("", 본문)):
        for 호, 호조각 in (_호_나누기(조각) if 호분할 else [(None, 조각)]):
            out.append({"id": f"조:{문서키}#{조번호}{항 or ''}{호 or ''}", "머리": 머리, "본문": 호조각, "원문결함": False})
    return out


def _덮어쓰기_읽기(문서키: str) -> dict:
    p = 저장소 / "ontology" / "추출" / "복원" / f"{문서키}.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8")).get("덮어쓰기") or {}


def 기존노드(사업명: str | None = None) -> list[str]:
    """이전 묶음에서 확정돼 원천 JSON 에 들어간 개념 ID 목록(5-7b). 새 묶음이 같은 개념을 다시 만들지 않게 한다.
    이 사업이 쓸 수 있는 것만 준다(공통 · 이 사업). 다른 사업 전용 개념은 이 사업 조회에서 보이지 않는다(ontology_lookup.py:56)."""
    ids: set[str] = set()
    for 파일 in sorted((저장소 / "ontology").glob("*.json")):
        if 파일.name == "schema.json":
            continue
        ids.update(c["id"] for c in json.loads(파일.read_text(encoding="utf-8")).get("개념") or []
                   if c.get("적용사업") in (None, 사업명))
    return sorted(ids)


def 만들기(폴더: Path) -> dict:
    명세 = json.loads((폴더 / "입력_명세.json").read_text(encoding="utf-8"))
    스키마 = json.loads((저장소 / "ontology" / "schema.json").read_text(encoding="utf-8"))
    블록: list[dict] = []
    with db.connect(connect_timeout=5, autocommit=True) as conn:
        cur = conn.cursor()
        for 문서 in 명세["문서"]:
            키 = 문서["문서키"]
            doc_id = 스키마["문서키"][키]
            덮어쓰기 = _덮어쓰기_읽기(키)
            행 = cur.execute(
                'SELECT "조번호", "조제목", "본문" FROM corpus.doc_articles '
                'WHERE doc_id = %s AND NOT coalesce("삭제", false) ORDER BY "조순번" NULLS LAST, article_id',
                (doc_id,)).fetchall()
            지정 = 문서.get("조")
            접두 = 문서.get("조접두")
            고른 = [r for r in 행 if (지정 is None and 접두 is None) or (지정 and r[0] in 지정)
                  or (접두 and r[0].startswith(tuple(접두)))]
            if 지정:
                없음 = [x for x in 지정 if x not in {r[0] for r in 행}]
                if 없음:
                    raise SystemExit(f"{키}: DB 에 없는 조 {없음}")
                순서 = {x: i for i, x in enumerate(지정)}
                고른.sort(key=lambda r: 순서[r[0]])
            호분할 = set(문서.get("호분할") or [])
            블록접두 = 문서.get("블록접두") or {}             # {"붙임2": ["붙임2/인건비"]}: 그 조에서 이 접두의 블록만(재추출 대상을 좁힌다)
            for 조번호, 조제목, 본문 in 고른:
                새블록 = 블록_만들기(키, 조번호, 조제목, 본문 or "", 호분할=조번호 in 호분할, 덮어쓰기=덮어쓰기)
                if 조번호 in 블록접두:
                    새블록 = [b for b in 새블록 if b["id"].startswith(tuple(f"조:{키}#{x}" for x in 블록접두[조번호]))]
                블록 += 새블록
    줄 = [f"# 입력 원문 — {폴더.name}", "",
          "각 머리말이 조문 ID 다. 발췌는 이 파일의 본문에서만 따온다. `[원문결함]` 은 DB 본문과 PDF 가 달라 PDF 복원본을 쓴 조문이다.", ""]
    if 명세.get("인용대응"):         # 옛 판 조번호를 인용하는 규정: 조문 ID 는 현행 판 번호로 쓴다(13차 → 14차, ontology/추출/예비/범위.md)
        줄 += ["이 규정이 인용한 다른 문서의 조번호는 옛 판 번호다. 관계의 조문 ID 는 아래 오른쪽 번호를 쓰고, 발췌는 원문 표기를 그대로 둔다.", ""]
        줄 += [f"- {x}" for x in 명세["인용대응"]] + [""]
    for b in 블록:
        줄 += [f"### {b['id']}", f"<!-- {b['머리']}{' [원문결함]' if b['원문결함'] else ''} -->", b["본문"], ""]
    (폴더 / "입력_원문.md").write_text("\n".join(줄), encoding="utf-8")
    (폴더 / "입력_문서키.json").write_text(json.dumps(
        {"사업키": {명세["사업키"]: 스키마["사업키"][명세["사업키"]]},
         "문서키": {d["문서키"]: 스키마["문서키"][d["문서키"]] for d in 명세["문서"]},
         "문서 표시명": {d["문서키"]: 스키마["문서 표시명"][d["문서키"]] for d in 명세["문서"]}},
        ensure_ascii=False, indent=1), encoding="utf-8")
    기존 = 폴더 / "입력_기존노드.json"
    if not 기존.exists():
        기존.write_text(json.dumps(기존노드(스키마["사업키"][명세["사업키"]]), ensure_ascii=False, indent=0) + "\n", encoding="utf-8")
    return {"블록": len(블록), "조문ID": [b["id"] for b in 블록]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("폴더")
    r = 만들기(Path(ap.parse_args().폴더))
    print(f"블록 {r['블록']}")
    for i in r["조문ID"]:
        print(i)
    return 0


if __name__ == "__main__":
    sys.exit(main())
