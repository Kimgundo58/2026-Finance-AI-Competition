# -*- coding: utf-8 -*-
"""OpenAPI 명세를 파일로 뽑는다 — SwaggerHub 등 외부 도구에 올릴 용도.

    PYTHONIOENCODING=utf-8 python scripts/export_openapi.py
    PYTHONIOENCODING=utf-8 python scripts/export_openapi.py --공개용   # /admin/* 제외

FastAPI 가 `/openapi.json` 으로 이미 내주지만 **서버를 띄워야 보인다.** 파일로 떨궈
레포에 두면 서버 없이도 계약을 읽을 수 있고, 계약이 언제 바뀌었는지 git 이 기록한다.

■ 왜 3.0.3 도 같이 뽑나
  FastAPI 는 OpenAPI **3.1.0** 을 낸다. SwaggerHub·일부 코드생성기는 3.0.x 를 더
  안정적으로 먹는다. 3.1 이 `type: ["string","null"]`·`anyOf[..., null]` 로 쓰는 것을
  3.0 의 `nullable: true` 로 낮춘 판을 같이 낸다. 🔴 **낮춘 판은 파생물이다** —
  계약의 정본은 3.1 쪽이다.

■ 🔴 `--공개용`
  `/admin/*` 4개는 운영용이다(x-admin-token 헤더). 외부에 계약을 공개할 때
  «관리 엔드포인트가 있다» 는 사실 자체를 안 알리고 싶으면 이 옵션을 쓴다.
  프론트 계약에는 `/api/*` 만 있으면 된다.
"""
from __future__ import annotations

import argparse
import copy
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SUDDOE_MOCK", "1")          # DB 없이도 스펙은 나온다


def 낮추기(s: dict) -> dict:
    """3.1 → 3.0.3. 스키마 트리를 훑어 null 표현만 바꾼다."""
    s = copy.deepcopy(s)
    s["openapi"] = "3.0.3"

    def 걷기(o):
        if isinstance(o, list):
            for x in o:
                걷기(x)
            return
        if not isinstance(o, dict):
            return
        # anyOf 안에 {"type":"null"} 이 섞인 형태 → nullable
        any_ = o.get("anyOf")
        if isinstance(any_, list):
            널 = [x for x in any_ if isinstance(x, dict) and x.get("type") == "null"]
            남 = [x for x in any_ if not (isinstance(x, dict) and x.get("type") == "null")]
            if 널 and len(남) == 1:
                o.pop("anyOf")
                o.update(남[0])
                o["nullable"] = True
            elif 널:
                o["anyOf"] = 남
                o["nullable"] = True
        # type: ["string","null"] → type: string + nullable
        t = o.get("type")
        if isinstance(t, list):
            남 = [x for x in t if x != "null"]
            o["type"] = 남[0] if 남 else "object"
            if len(남) != len(t):
                o["nullable"] = True
        # 3.1 은 exclusiveMinimum 이 숫자, 3.0 은 boolean + minimum
        for 키, 짝 in (("exclusiveMinimum", "minimum"), ("exclusiveMaximum", "maximum")):
            if isinstance(o.get(키), (int, float)):
                o[짝] = o.pop(키)
                o[키] = True
        for v in o.values():
            걷기(v)

    걷기(s)
    return s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--공개용", action="store_true", help="/admin/* 을 뺀다")
    ap.add_argument("--출력", default="docs/7_백엔드", help="저장 폴더")
    a = ap.parse_args()

    from server.main import app
    s = app.openapi()

    이름 = "openapi"
    if a.공개용:
        s = copy.deepcopy(s)
        뺀것 = [p for p in s["paths"] if p.startswith("/admin")]
        for p in 뺀것:
            del s["paths"][p]
        이름 = "openapi_공개용"
        print(f"   /admin/* {len(뺀것)}개 제외: {뺀것}")

    os.makedirs(a.출력, exist_ok=True)
    for 접미, 문서 in (("", s), ("_3.0.3", 낮추기(s))):
        메서드 = sum(len([m for m in v if m in
                        ("get", "post", "put", "patch", "delete")])
                   for v in 문서["paths"].values())
        for 확장 in ("json", "yaml"):
            경로 = f"{a.출력}/{이름}{접미}.{확장}"
            if 확장 == "json":
                본문 = json.dumps(문서, ensure_ascii=False, indent=1) + "\n"
            else:
                # SwaggerHub 에디터가 YAML 네이티브다. 한글을 이스케이프하지 않는다.
                import yaml
                본문 = yaml.safe_dump(문서, allow_unicode=True, sort_keys=False,
                                     default_flow_style=False, width=100)
            io.open(경로, "w", encoding="utf-8").write(본문)
            print(f"   {경로}  OpenAPI {문서['openapi']} · "
                  f"경로 {len(문서['paths'])} · 메서드 {메서드} · "
                  f"{os.path.getsize(경로):,}바이트")


if __name__ == "__main__":
    main()
