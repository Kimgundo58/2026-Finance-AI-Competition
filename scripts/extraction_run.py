#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""추출 묶음의 작업 폴더 준비 · 입력 폴더 검사 · 실행기록 (docs/5_룰/5-12_다른_사업_추출.md 「미구현」, 5-7b 「입력 묶음」).

추출자마다 입력 3개만 든 작업 폴더(<묶음>/작업_A/ · 작업_B/)에서 돌리고, 끝나면 추출 파일을 묶음 폴더로 옮기며 실행기록.json 을 남긴다.
DB · LLM 을 쓰지 않는다. 프롬프트 본문은 5-7b 「에이전트 프롬프트」 코드 블록을 파일에서 읽어 글자 그대로 넣는다.

    python scripts/extraction_run.py 준비 <묶음 폴더> --사업폴더키 예비 [--상한 40]
    python scripts/extraction_run.py 검사 <작업 폴더>
    python scripts/extraction_run.py 마감 <묶음 폴더> --로그 A=<jsonl> --로그 B=<jsonl> [--effort A=high]
    python scripts/extraction_run.py 마감 <묶음 폴더> --에이전트 A=<ID>,<모델>,<effort>[,<시작>,<끝>] --에이전트 B=…
    python scripts/extraction_run.py --self-test

공개: 입력폴더_검사(폴더) -> list[str]  (.claude/작업/api_agents/run.py 가 추출 시작 직전에 부른다)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")

저장소 = Path(__file__).resolve().parent.parent
지침_파일 = 저장소 / "docs" / "5_룰" / "5-7b_추출_에이전트_지침.md"
입력파일 = ("입력_원문.md", "입력_문서키.json", "입력_기존노드.json")
추출자들 = ("A", "B")
# 추출자가 열어도 되는 저장소 문서(5-7b 프롬프트) + 시작할 때 자동으로 읽히는 CLAUDE.md
허용_문서 = ("docs/5_룰/5-5_규범_구조.md", "docs/5_룰/5-7_원문_추출_절차.md", "docs/5_룰/5-7b_추출_에이전트_지침.md",
          "docs/5_룰/5-8b_원천_JSON_형식.md", "CLAUDE.md")
미확인 = "미확인"


class 실행오류(Exception):
    """준비 · 마감을 멈춰야 할 때."""


def _해시(경로: Path) -> str:
    return hashlib.sha256(Path(경로).read_bytes()).hexdigest()


def _글해시(글: str) -> str:
    return hashlib.sha256(글.encode("utf-8")).hexdigest()


def _지금() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _경로표시(p: Path) -> str:
    """프롬프트 · 기록에 쓰는 경로. 저장소 안이면 상대 경로(PC 가 달라도 프롬프트 해시가 같다), 밖이면 절대 경로."""
    p = Path(p).resolve()
    try:
        return p.relative_to(저장소).as_posix()
    except ValueError:
        return p.as_posix()


# 입력 폴더 검사 ─────────────────────────────────────────────────────────────

def 입력폴더_검사(폴더: Path | str) -> list[str]:
    """작업 폴더에 입력 3개만 있는가. 어긋난 점을 돌려준다(빈 목록 = 통과). 하위 폴더 · 숨김 파일도 어긋남이다."""
    폴더 = Path(폴더)
    if not 폴더.is_dir():
        return [f"작업 폴더가 없다: {폴더}"]
    어긋남 = []
    있는 = {p.name: p for p in 폴더.iterdir()}
    for 이름 in 입력파일:
        if 이름 not in 있는:
            어긋남.append(f"입력 없음: {이름}")
        elif not 있는[이름].is_file():
            어긋남.append(f"입력이 파일이 아니다: {이름}")
    for 이름 in sorted(set(있는) - set(입력파일)):
        어긋남.append(f"입력 밖 {'폴더' if 있는[이름].is_dir() else '파일'}: {이름}")
    return 어긋남


def _파일목록(폴더: Path) -> list[dict]:
    return [{"이름": p.name, "sha256": _해시(p) if p.is_file() else None, **({} if p.is_file() else {"종류": "폴더"})}
            for p in sorted(Path(폴더).iterdir(), key=lambda x: x.name)]


# 프롬프트 ────────────────────────────────────────────────────────────────────

def 지침_프롬프트(지침: Path = 지침_파일) -> str:
    """5-7b 「에이전트 프롬프트」 절의 첫 코드 블록 본문(울타리 줄 제외)."""
    줄들 = Path(지침).read_text(encoding="utf-8").splitlines()
    i = next((k for k, s in enumerate(줄들) if s.startswith("## 에이전트 프롬프트")), None)
    if i is None:
        raise 실행오류(f"「에이전트 프롬프트」 절이 없다: {지침}")
    시작 = next((k for k in range(i + 1, len(줄들)) if 줄들[k].startswith("```")
               or (줄들[k].startswith("## ") and k > i)), None)
    if 시작 is None or not 줄들[시작].startswith("```"):
        raise 실행오류(f"「에이전트 프롬프트」 절에 코드 블록이 없다: {지침}")
    끝 = next((k for k in range(시작 + 1, len(줄들)) if 줄들[k].startswith("```")), None)
    if 끝 is None:
        raise 실행오류(f"코드 블록이 닫히지 않았다: {지침}")
    return "\n".join(줄들[시작 + 1:끝])


def 덧붙인_문장(작업폴더: Path, 추출자: str) -> tuple[str, str]:
    """코드 블록 앞뒤에 붙이는 글. 앞: 입력 폴더 · 추출자 글자 · 독립 작업 · 입력 파일이 가리키는 곳. 뒤: 출력 형식 절 안내 · 300k."""
    폴더 = _경로표시(작업폴더)
    앞 = (f"Input folder: {폴더}\n"
         f"You are extractor {추출자}. Work independently: another extractor works on the same provisions; do not look for, "
         f"open or ask about its folder, its output or any file outside the input folder except the documents the prompt below allows.\n"
         f"In the prompt below, 입력_원문.md, 입력_문서키.json, 입력_기존노드.json and \"the input folder\" mean the files in {폴더}.")
    뒤 = (f"Output format: follow the section 출력 형식 of docs/5_룰/5-7b_추출_에이전트_지침.md exactly, "
         f"and write the output as {폴더}/추출_{추출자}.md.\n"
         f"Keep this session under 300k tokens of context.")
    return 앞, 뒤


def 프롬프트_만들기(작업폴더: Path, 추출자: str, 본문: str) -> str:
    앞, 뒤 = 덧붙인_문장(작업폴더, 추출자)
    return f"{앞}\n\n{본문}\n\n{뒤}\n"


def _프롬프트_나누기(프롬프트: str, 본문: str) -> dict | None:
    """프롬프트 = 앞 + 본문 + 뒤 인지 보고 앞 · 뒤를 돌려준다. 본문이 글자 그대로 한 번 들어 있지 않으면 None."""
    if 프롬프트.count(본문) != 1:
        return None
    앞, 뒤 = 프롬프트.split(본문)
    return {"앞": 앞.rstrip("\n"), "뒤": 뒤.strip("\n")}


# 준비 ────────────────────────────────────────────────────────────────────────

def _같은_내용(파일: Path, 글: str) -> bool:
    return 파일.exists() and 파일.read_text(encoding="utf-8") == 글


def _쓰기(파일: Path, 글: str) -> None:
    """이미 있고 내용이 다르면 멈춘다(실행 뒤 프롬프트가 바뀌면 해시가 실제와 어긋난다)."""
    if 파일.exists() and not _같은_내용(파일, 글):
        raise 실행오류(f"이미 있고 내용이 다르다: {파일}")
    파일.write_text(글, encoding="utf-8", newline="\n")


def 준비(묶음: Path, 사업폴더키: str, 상한: float = 40.0, 지침: Path = 지침_파일) -> dict:
    묶음 = Path(묶음).resolve()
    없음 = [x for x in 입력파일 if not (묶음 / x).is_file()]
    if 없음:
        raise 실행오류(f"묶음 폴더에 입력이 없다: {없음}")
    본문 = 지침_프롬프트(지침)
    작업들 = []
    for X in 추출자들:
        작업 = 묶음 / f"작업_{X}"
        if 작업.exists():
            어긋남 = 입력폴더_검사(작업)
            다름 = [x for x in 입력파일 if (작업 / x).is_file() and _해시(작업 / x) != _해시(묶음 / x)]
            if 어긋남 or 다름:
                raise 실행오류(f"작업_{X} 가 이미 있고 내용이 다르다: {어긋남 + [f'내용 다름: {x}' for x in 다름]}")
        else:
            작업.mkdir()
            for x in 입력파일:
                shutil.copyfile(묶음 / x, 작업 / x)
        프롬프트 = 묶음 / f"프롬프트_{X}.md"
        _쓰기(프롬프트, 프롬프트_만들기(작업, X, 본문))
        작업들.append({"이름": f"{사업폴더키}_{묶음.name}_{X}", "프롬프트": str(프롬프트), "상한": 상한, "입력검사": str(작업)})
    # 한 단계(목록) 안의 두 작업은 run.py 가 동시에 돌린다
    _쓰기(묶음 / "작업.json", json.dumps([작업들], ensure_ascii=False, indent=1) + "\n")
    return {"작업": 작업들}


# 로그 읽기 ───────────────────────────────────────────────────────────────────

def _허용경로(작업폴더: Path, 추출자: str) -> set[str]:
    경로 = [Path(작업폴더) / x for x in 입력파일] + [Path(작업폴더) / f"추출_{추출자}.md"] + [저장소 / x for x in 허용_문서]
    return {os.path.normcase(os.path.abspath(p)) for p in 경로}


def _안쪽(경로: str, 기준: str | None) -> str:
    p = Path(경로)
    if not p.is_absolute():
        p = Path(기준 or 저장소) / p
    return os.path.normcase(os.path.abspath(p))


# 묶음 폴더에 있어 추출자가 보면 안 되는 파일(5-12 독립성: 대상 목록 · 다른 추출 · 비교 · 확정)
묶음_비공개 = ("입력_명세.json", "재추출_대상.md", "메모.md", "비교.md", "확정.md")
_경로글자 = r"[^\s\"'`;|&<>()]*"
_저장소경로 = re.compile(r"(?:(?<=[\s\"'/=])|^)(ontology|\.claude|docs|scripts)/" + _경로글자)


def 명령_허용밖(명령: str, 작업폴더: Path, 추출자: str) -> list[str]:
    """Bash 명령 글에서 허용 밖 경로를 찾는다(문자열 검사). 돌려주는 것은 찾은 경로 조각들(빈 목록 = 허용 밖 없음).
    허용 밖: 묶음 폴더의 비공개 파일 이름, 다른 추출자의 작업 폴더 · 추출 파일, 자기 작업 폴더 밖의 `ontology/` 경로(원천 JSON ·
    다른 추출 폴더), `.claude/` 기록 폴더, 허용 문서 밖의 `docs/`, `scripts/`(저장소 코드는 원천을 읽는다), 묶음 폴더 자체(작업 폴더 밖).
    `$TEMP` 에 만든 자기 점검 스크립트는 경로 검사 대상이 아니다(heredoc 본문은 명령 글이라 함께 검사된다).
    한계: `..` 처럼 경로 이름 없이 올라가는 것은 잡지 못한다."""
    글 = str(명령 or "").replace("\\", "/")
    작업 = Path(작업폴더).resolve()
    자기 = _경로표시(작업)
    out: list[str] = []

    def 넣기(x: str) -> None:
        if x not in out:
            out.append(x)
    다른 = [X for X in 추출자들 if X != 추출자]
    이름식 = re.compile("|".join([re.escape(x) for x in 묶음_비공개]
                              + [rf"작업_{X}(?![\w])" for X in 다른] + [rf"추출_{X}\.md" for X in 다른]))
    for m in 이름식.finditer(글):
        넣기(m.group(0))
    for m in _저장소경로.finditer(글):
        p, 맨앞 = m.group(0), m.group(1)
        if 맨앞 == "ontology" and (p.rstrip("/") == 자기 or p.startswith(자기 + "/")):
            continue
        if 맨앞 == "docs" and p in 허용_문서:
            continue
        넣기(p)
    묶음글 = 작업.parent.as_posix()
    for m in re.finditer(re.escape(묶음글) + _경로글자, 글):
        나머지 = m.group(0)[len(묶음글):].rstrip("/")
        if not (나머지 == f"/{작업.name}" or 나머지.startswith(f"/{작업.name}/")):
            넣기(m.group(0))
    return out


def 로그_읽기(로그: Path, 작업폴더: Path, 추출자: str) -> dict:
    """claude -p --output-format stream-json 로그에서 세션 · 모델 · effort · 읽은 파일 · 명령 · 시각 · 비용을 읽는다."""
    세션들: list[str] = []
    모델들: list[str] = []
    effort = None
    cwd = None
    인증 = None
    읽은: list[str] = []
    명령: list[dict] = []
    허용밖: list[str] = []
    시각: list[str] = []
    비용 = None
    작업읽기 = 0
    허용 = _허용경로(작업폴더, 추출자)
    작업경로 = os.path.normcase(os.path.abspath(작업폴더)) + os.sep
    for 줄 in Path(로그).read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(줄)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        if d.get("timestamp"):
            시각.append(str(d["timestamp"]))
        if d.get("type") == "system" and d.get("subtype") == "init":
            if d.get("session_id") and d["session_id"] not in 세션들:
                세션들.append(d["session_id"])
            if d.get("model") and d["model"] not in 모델들:
                모델들.append(d["model"])
            cwd = d.get("cwd") or cwd
            인증 = d.get("apiKeySource") or 인증
            effort = d.get("effort") or effort        # 지금 CLI 는 init 에 effort 를 적지 않는다. 있으면 쓴다
        elif d.get("type") == "assistant":
            m = (d.get("message") or {})
            if m.get("model") and m["model"] not in 모델들:
                모델들.append(m["model"])
            for c in m.get("content") or []:
                if not isinstance(c, dict) or c.get("type") != "tool_use":
                    continue
                입력 = c.get("input") or {}
                이름 = c.get("name")
                if 이름 == "Read":
                    p = str(입력.get("file_path") or "")
                    읽은.append(p)
                    if _안쪽(p, cwd) not in 허용:
                        허용밖.append(p)
                    if _안쪽(p, cwd).startswith(작업경로):
                        작업읽기 += 1
                elif 이름 == "Bash":
                    명령.append({"도구": "Bash", "명령": 입력.get("command")})
                    허용밖 += [f"Bash {x}" for x in 명령_허용밖(str(입력.get("command") or ""), 작업폴더, 추출자)]
                elif 이름 in ("Grep", "Glob"):
                    # 검색도 파일을 읽는다. 경로가 허용 파일 하나가 아니면(폴더 · 저장소 전체) 허용 밖으로 적는다
                    명령.append({"도구": 이름, **{k: 입력.get(k) for k in ("pattern", "path", "glob") if 입력.get(k)}})
                    p = str(입력.get("path") or (cwd or 저장소))
                    if _안쪽(p, cwd) not in 허용:
                        허용밖.append(f"{이름} {p}")
        elif d.get("type") == "result":
            if d.get("total_cost_usd") is not None:
                비용 = (비용 or 0.0) + float(d["total_cost_usd"])
    return {"에이전트ID": 세션들[0] if len(세션들) == 1 else (세션들 or 미확인), "모델": 모델들[0] if len(모델들) == 1 else (모델들 or 미확인),
            "effort": effort or 미확인, "시작": min(시각) if 시각 else 미확인, "끝": max(시각) if 시각 else 미확인,
            "읽은파일": 읽은, "명령": 명령, "허용밖읽기": 허용밖, "작업폴더_Read": 작업읽기, "비용": 비용, "인증": 인증,
            "로그": _경로표시(Path(로그))}


# 마감 ────────────────────────────────────────────────────────────────────────

def _짝(값들: list[str] | None, 이름: str) -> dict[str, str]:
    out = {}
    for 값 in 값들 or []:
        if "=" not in 값:
            raise 실행오류(f"{이름} 는 X=값 꼴이다: {값}")
        k, v = 값.split("=", 1)
        if k not in 추출자들:
            raise 실행오류(f"{이름} 의 추출자는 A · B 다: {k}")
        out[k] = v
    return out


def 마감(묶음: Path, 로그: dict[str, str] | None = None, 에이전트: dict[str, str] | None = None,
       effort: dict[str, str] | None = None, 지침: Path = 지침_파일) -> tuple[bool, dict]:
    """작업 폴더의 추출 파일을 묶음 폴더로 옮기고 실행기록.json 을 쓴다. 어긋남이 하나라도 있으면 옮기지 않고 (False, 기록)."""
    묶음 = Path(묶음).resolve()
    로그, 에이전트, effort = 로그 or {}, 에이전트 or {}, effort or {}
    if set(로그) & set(에이전트):
        raise 실행오류("한 추출자에 --로그 와 --에이전트 를 함께 줄 수 없다")
    이전 = 묶음 / "실행기록.json"
    if 이전.is_file() and json.loads(이전.read_text(encoding="utf-8")).get("통과"):
        raise 실행오류(f"이미 마감한 묶음이다. 기록을 덮어쓰지 않는다: {_경로표시(이전)}")
    본문 = 지침_프롬프트(지침)
    기록 = {"묶음": _경로표시(묶음), "마감": _지금(), "추출자": {}}
    옮길 = []
    for X in 추출자들:
        작업 = 묶음 / f"작업_{X}"
        추출 = 작업 / f"추출_{X}.md"
        문제: list[str] = []
        행: dict = {}
        if X in 로그:
            행.update(로그_읽기(Path(로그[X]), 작업, X))
            행["경로"] = "API"
            # 지금 CLI 로그에는 effort 가 없다. 로그에 있으면 로그 값, 없으면 --effort 인자 값이고 어디서 왔는지 적는다(5-12)
            if 행["effort"] != 미확인:
                행["effort_출처"] = "로그"
                if X in effort and effort[X] != 행["effort"]:
                    문제.append(f"effort 가 로그({행['effort']})와 인자({effort[X]})에서 다르다")
            elif X in effort:
                행["effort"], 행["effort_출처"] = effort[X], "인자"
            else:
                행["effort_출처"] = 미확인
            if isinstance(행["에이전트ID"], list):
                문제.append(f"로그의 세션 ID 가 둘 이상이다(같은 로그에 여러 실행이 섞였다): {행['에이전트ID']}")
            elif 행["에이전트ID"] == 미확인:
                문제.append("로그에 세션 ID 가 없다")
            if not 행["작업폴더_Read"]:
                문제.append(f"로그의 Read 경로가 작업_{X} 를 가리키지 않는다(다른 추출자의 로그일 수 있다)")
            if 행["허용밖읽기"]:
                문제.append(f"허용 밖 읽기 {len(행['허용밖읽기'])}건: {행['허용밖읽기'][:3]}")
        elif X in 에이전트:
            칸 = [x.strip() for x in 에이전트[X].split(",")]
            if len(칸) not in (3, 5):
                raise 실행오류(f"--에이전트 {X} 는 <ID>,<모델>,<effort>[,<시작>,<끝>] 이다: {에이전트[X]}")
            행.update({"에이전트ID": 칸[0] or 미확인, "모델": 칸[1] or 미확인, "effort": 칸[2] or 미확인,
                      "effort_출처": "인자" if 칸[2] else 미확인,
                      "시작": 칸[3] if len(칸) == 5 else 미확인, "끝": 칸[4] if len(칸) == 5 else 미확인,
                      "읽은파일": 미확인, "명령": 미확인, "허용밖읽기": 미확인, "비용": None, "경로": "구독"})
        else:
            raise 실행오류(f"추출자 {X} 의 --로그 또는 --에이전트 가 없다")
        프롬프트 = 묶음 / f"프롬프트_{X}.md"
        행["프롬프트해시_출처"] = "파일"          # 로그에는 프롬프트 글이 없다. 마감 때 프롬프트_X.md 의 해시다
        if 프롬프트.is_file():
            글 = 프롬프트.read_text(encoding="utf-8")
            행["프롬프트해시"] = _글해시(글)
            행["덧붙인문장"] = _프롬프트_나누기(글, 본문)
            if 행["덧붙인문장"] is None:
                문제.append("프롬프트에 5-7b 코드 블록이 글자 그대로 한 번 들어 있지 않다")
        else:
            행["프롬프트해시"] = 행["덧붙인문장"] = None
            문제.append(f"프롬프트 파일 없음: {프롬프트.name}")
        if 작업.is_dir():
            행["입력폴더"] = _파일목록(작업)
            행["추가파일"] = sorted(p.name for p in 작업.iterdir() if p.name not in (*입력파일, 추출.name))
            for x in 입력파일:
                if not (작업 / x).is_file():
                    문제.append(f"입력 없음: {x}")
                elif not (묶음 / x).is_file() or _해시(작업 / x) != _해시(묶음 / x):
                    문제.append(f"입력이 묶음 폴더의 것과 다르다: {x}")
        else:
            행["입력폴더"], 행["추가파일"] = [], []
            문제.append(f"작업 폴더 없음: 작업_{X}")
        if 행["추가파일"]:
            문제.append(f"입력 3개와 추출 파일 밖의 파일: {행['추가파일']}")
        if not 추출.is_file():
            문제.append(f"추출 파일 없음: 작업_{X}/{추출.name}")
        if (묶음 / 추출.name).exists():
            문제.append(f"묶음 폴더에 이미 {추출.name} 가 있다")
        행["문제"] = 문제
        기록["추출자"][X] = 행
        옮길.append((추출, 묶음 / 추출.name))
    통과 = not any(행["문제"] for 행 in 기록["추출자"].values())
    기록["통과"] = 통과
    if 통과:
        for 원래, 새 in 옮길:
            shutil.move(str(원래), str(새))
    (묶음 / "실행기록.json").write_text(json.dumps(기록, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    return 통과, 기록


# self-test ──────────────────────────────────────────────────────────────────

def _입력_넣기(폴더: Path) -> None:
    폴더.mkdir(parents=True, exist_ok=True)
    (폴더 / "입력_원문.md").write_text("### 조:시험#제1조\n본문\n", encoding="utf-8")
    (폴더 / "입력_문서키.json").write_text('{"사업키": {}}\n', encoding="utf-8")
    (폴더 / "입력_기존노드.json").write_text("[]\n", encoding="utf-8")


def _시험_로그(경로: Path, 작업: Path, 묶음: Path) -> None:
    def 도구(이름, 입력):
        return {"type": "assistant", "timestamp": "2026-11-08T10:00:05.000Z", "session_id": "세션-1",
                "message": {"model": "claude-opus-5-5", "content": [{"type": "tool_use", "name": 이름, "input": 입력}]}}
    줄 = ["⚠ JSON 이 아닌 줄",
         {"type": "system", "subtype": "init", "session_id": "세션-1", "model": "claude-opus-5-5", "cwd": str(저장소),
          "apiKeySource": "ANTHROPIC_API_KEY"},
         {"type": "assistant", "timestamp": "2026-11-08T10:00:01.000Z", "message": {"model": "claude-opus-5-5", "content": []}},
         도구("Read", {"file_path": str(저장소 / "docs" / "5_룰" / "5-5_규범_구조.md")}),
         도구("Read", {"file_path": str(작업 / "입력_원문.md")}),
         도구("Read", {"file_path": "docs/5_룰/5-8b_원천_JSON_형식.md"}),
         도구("Read", {"file_path": str(묶음 / "재추출_대상.md")}),
         도구("Read", {"file_path": str(저장소 / "ontology" / "모두의창업.json")}),
         도구("Bash", {"command": "ls"}),
         도구("Grep", {"pattern": "금지"}),
         {"type": "assistant", "timestamp": "2026-11-08T10:09:00.000Z", "message": {"model": "claude-opus-5-5", "content": []}},
         {"type": "result", "subtype": "success", "total_cost_usd": 1.25, "session_id": "세션-1"}]
    경로.write_text("\n".join(x if isinstance(x, str) else json.dumps(x, ensure_ascii=False) for x in 줄) + "\n", encoding="utf-8")


def _로그_쓰기(경로: Path, 세션: str, 도구들: list[tuple[str, dict]], 둘째세션: str | None = None) -> Path:
    """stream-json 꼴 시험 로그. 둘째세션 이 있으면 같은 파일에 실행 하나를 더 붙인다(run.py 로그는 `a` 모드)."""
    def 실행(s: str) -> list[dict]:
        return ([{"type": "system", "subtype": "init", "session_id": s, "model": "claude-opus-5-5", "cwd": str(저장소)}]
                + [{"type": "assistant", "timestamp": "2026-11-08T10:00:05.000Z", "session_id": s,
                    "message": {"model": "claude-opus-5-5", "content": [{"type": "tool_use", "name": 이름, "input": 입력}]}}
                   for 이름, 입력 in 도구들]
                + [{"type": "result", "subtype": "success", "total_cost_usd": 1.0, "session_id": s}])
    줄 = 실행(세션) + (실행(둘째세션) if 둘째세션 else [])
    경로.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in 줄) + "\n", encoding="utf-8")
    return 경로


# .claude/작업/api_agents/runs/예비_1_예비_A.jsonl 의 Bash 명령 5개를 글자 그대로 옮긴 것(2026-11-07 실행, 마감 통과한 실제 꼴)
_실제_A_명령: list[str] = json.loads(r'''["ls -la \"ontology/추출/예비/1_예비/작업_A\"", "cd \"ontology/추출/예비/1_예비/작업_A\" && grep -c '^### ' 입력_원문.md && python -c \"print(len(open('입력_기존노드.json',encoding='utf-8').read()))\"", "mkdir -p \"$TEMP/extract_a_tools\" && cat > \"$TEMP/extract_a_tools/fill_check.py\" <<'EOF'\nimport re, sys, unicodedata\ninp, out = sys.argv[1], sys.argv[2]\ndef norm(s):\n    s = unicodedata.normalize('NFC', s)\n    for q in '“”″': s = s.replace(q, '\"')\n    for q in '‘’': s = s.replace(q, \"'\")\n    s = re.sub(r'-\\s*\\d+\\s*-', ' ', s) if False else s\n    s = re.sub(r'(?m)^\\s*-\\s*\\d+\\s*-\\s*$', ' ', s)\n    s = re.sub(r'\\s+', ' ', s).strip()\n    return s\ndef blocks(text):\n    parts = re.split(r'(?m)^### (.+)$', text)\n    return [(parts[i].strip(), parts[i+1]) for i in range(1, len(parts), 2)]\nsrc = blocks(open(inp, encoding='utf-8').read())\nbody = {}\nraw_tbl = {}\nfor k, v in src:\n    lines = [l for l in v.splitlines() if not l.strip().startswith('<!--')]\n    body[k] = norm('\\n'.join(lines))\n    # 원문 line: table rows -> cells joined by ' / '\n    rl = []\n    for l in lines:\n        if re.match(r'^\\s*\\|[-| ]+\\|\\s*$', l): continue\n        if l.strip().startswith('|'):\n            cells = [c.strip() for c in l.strip().strip('|').split('|')]\n            l = ' / '.join(c for c in cells if c)\n        rl.append(l)\n    raw_tbl[k] = norm('\\n'.join(rl))\ntxt = open(out, encoding='utf-8').read()\nob = blocks(txt)\nids_in = [k for k,_ in src]; ids_out = [k for k,_ in ob]\nprint('input', len(ids_in), 'output', len(ids_out), 'same order', ids_in == ids_out)\nprint('missing', [i for i in ids_in if i not in ids_out], 'extra', [i for i in ids_out if i not in ids_in])\n# fill\ndef fill(m):\n    return m.group(0)\nnewparts = re.split(r'(?m)^(### .+)$', txt)\nres = [newparts[0]]\nfor i in range(1, len(newparts), 2):\n    hid = newparts[i][4:].strip(); blk = newparts[i+1]\n    if '원문: @@' in blk:\n        blk = blk.replace('원문: @@', '원문: ' + raw_tbl[hid], 1)\n    res += [newparts[i], blk]\ntxt2 = ''.join(res)\nopen(out, 'w', encoding='utf-8', newline='\\n').write(txt2)\n# checks\nbad = 0; concepts=set(); rels=0; norms=0; kinds={}\nfor hid, blk in blocks(txt2):\n    b = body[hid]\n    for line in blk.splitlines():\n        s = line.strip()\n        if s.startswith('원문:'): continue\n        if s.startswith('개념:'):\n            for m in re.finditer(r'([가-힣A-Za-z]+:[^\\s·()]+) \\((기존|신규)\\)', s): concepts.add(m.group(1))\n        if ' -' in s and '-> ' in s: rels += 1\n        m = re.match(r'(?:규범: )?(규범:\\S+)', s)\n        if m:\n            norms += 1; k = m.group(1).split('/')[-2]; kinds[k] = kinds.get(k,0)+1\n            for f in s.split(' | '):\n                if f.startswith('문장 ') and ('\"' in f): print('QUOTE IN 문장', hid, f); bad+=1\n        for ex in re.findall(r'\"([^\"]*)\"', s):\n            if norm(ex) not in b:\n                print('EXCERPT FAIL', hid, '::', ex); bad += 1\nprint('bad', bad, 'concepts', len(concepts), 'relations', rels, 'norms', norms, kinds)\nEOF\ncd \"ontology/추출/예비/1_예비/작업_A\" && PYTHONIOENCODING=utf-8 python -I \"$TEMP/extract_a_tools/fill_check.py\" 입력_원문.md 추출_A.md", "PYTHONIOENCODING=utf-8 python -I \"$TEMP/extract_a_tools/fill_check.py\" 입력_원문.md 추출_A.md", "grep -c '원문: @@' 추출_A.md; grep -n '^원문:.* | ' 추출_A.md | head; grep -n '^원문:' 추출_A.md | sed -n '22p;34p'"]''')


def _자체시험() -> int:
    결과: list[tuple[str, bool, str]] = []

    def 확인(이름: str, 참: bool, 보충: object = "") -> None:
        결과.append((이름, bool(참), "" if 참 else str(보충)))

    본문 = 지침_프롬프트()
    with tempfile.TemporaryDirectory() as 임시:
        임시 = Path(임시)
        # 입력 폴더 검사
        정상 = 임시 / "정상"
        _입력_넣기(정상)
        확인("입력 3개만 든 폴더는 통과", 입력폴더_검사(정상) == [], 입력폴더_검사(정상))
        for 이름, 바꿈, 기대 in (
                ("재추출_대상.md 를 넣은 폴더는 실패(5-12 완료 기준)", lambda p: (p / "재추출_대상.md").write_text("x", encoding="utf-8"), "재추출_대상.md"),
                ("하위 폴더가 있으면 실패", lambda p: (p / "참고").mkdir(), "참고"),
                ("숨김 파일이 있으면 실패", lambda p: (p / ".메모").write_text("x", encoding="utf-8"), ".메모"),
                ("입력이 빠지면 실패", lambda p: (p / "입력_기존노드.json").unlink(), "입력 없음: 입력_기존노드.json")):
            폴더 = 임시 / 이름[:6]
            _입력_넣기(폴더)
            바꿈(폴더)
            r = 입력폴더_검사(폴더)
            확인(이름, any(기대 in x for x in r), r)
        확인("없는 폴더는 실패", 입력폴더_검사(임시 / "없음") != [])

        # 준비
        묶음 = 임시 / "묶음01"
        _입력_넣기(묶음)
        (묶음 / "입력_명세.json").write_text("{}", encoding="utf-8")
        (묶음 / "재추출_대상.md").write_text("대상", encoding="utf-8")       # 묶음 폴더에 있어도 작업 폴더로 가지 않는다
        r = 준비(묶음, "시험")
        확인("준비한 작업 폴더 두 개는 검사를 통과", all(입력폴더_검사(묶음 / f"작업_{X}") == [] for X in 추출자들))
        프롬A = (묶음 / "프롬프트_A.md").read_text(encoding="utf-8")
        확인("프롬프트에 5-7b 코드 블록 글자가 그대로 들어 있다", 본문 in 프롬A and 본문 in 지침_파일.read_text(encoding="utf-8")
             and "You extract concepts" in 본문 and "```" not in 본문)
        확인("프롬프트는 작업 폴더 밖에 있다", not (묶음 / "작업_A" / "프롬프트_A.md").exists() and (묶음 / "프롬프트_B.md").is_file())
        작업json = json.loads((묶음 / "작업.json").read_text(encoding="utf-8"))
        확인("작업.json 은 run.py 항목(이름 · 프롬프트 · 상한 · 입력검사) 한 단계", len(작업json) == 1 and len(작업json[0]) == 2
             and all(set(x) == {"이름", "프롬프트", "상한", "입력검사"} for x in 작업json[0]), 작업json)
        준비(묶음, "시험")
        확인("같은 내용이면 준비를 다시 해도 된다", True)
        (묶음 / "작업_B" / "입력_원문.md").write_text("바뀐 본문", encoding="utf-8")
        try:
            준비(묶음, "시험")
            확인("작업 폴더 내용이 다르면 준비 실패", False, "멈추지 않음")
        except 실행오류:
            확인("작업 폴더 내용이 다르면 준비 실패", True)
        shutil.copyfile(묶음 / "입력_원문.md", 묶음 / "작업_B" / "입력_원문.md")

        # 로그 읽기
        로그A = 임시 / "A.jsonl"
        _시험_로그(로그A, 묶음 / "작업_A", 묶음)
        g = 로그_읽기(로그A, 묶음 / "작업_A", "A")
        확인("로그: 세션 · 모델", g["에이전트ID"] == "세션-1" and g["모델"] == "claude-opus-5-5", g)
        확인("로그: 읽은파일 5개 · 명령 2개", len(g["읽은파일"]) == 5 and [c["도구"] for c in g["명령"]] == ["Bash", "Grep"], g)
        확인("로그: 허용밖읽기 = 재추출_대상 · 원천 JSON · 저장소 전체 검색",
             len(g["허용밖읽기"]) == 3 and "재추출_대상.md" in g["허용밖읽기"][0] and "모두의창업.json" in g["허용밖읽기"][1]
             and g["허용밖읽기"][2].startswith("Grep "), g["허용밖읽기"])
        확인("로그: effort 가 없으면 미확인 · 시각 · 비용", g["effort"] == 미확인 and g["시작"].endswith("10:00:01.000Z")
             and g["끝"].endswith("10:09:00.000Z") and g["비용"] == 1.25, g)

        # Bash 명령의 허용 밖 경로(문자열 검사)
        작A = 묶음 / "작업_A"
        밖명령 = {
            "묶음의 재추출_대상": f"cat '{묶음.as_posix()}/재추출_대상.md'", "다른 추출자 폴더": "cat ../작업_B/추출_B.md",
            "입력_명세": "python -c \"print(open('../입력_명세.json').read())\"", "메모": "type ..\\메모.md", "비교": "head 비교.md",
            "확정": "grep 규범 확정.md", "원천 JSON": "cat ontology/공통_L1.json", "다른 추출 폴더": "ls ontology/추출/모두의창업/1_A_제34-37조",
            "기록 폴더": "tail .claude/작업/api_agents/runs/예비_1_예비_B.jsonl", "허용 밖 문서": "cat docs/5_룰/5-7d_확정표_형식.md",
            "저장소 코드": "python scripts/ontology_input.py --help", "묶음 폴더 자체": f"ls \"{묶음.as_posix()}\""}
        못잡음 = {k: c for k, c in 밖명령.items() if not 명령_허용밖(c, 작A, "A")}
        확인("Bash: 허용 밖 경로(묶음 비공개 파일 · 다른 추출자 · ontology · .claude · 다른 문서 · scripts · 묶음 폴더)를 찾는다", not 못잡음, 못잡음)
        안명령 = [f"ls -la \"{작A.as_posix()}\"", f"cd \"{작A.as_posix()}\" && grep -c '^### ' 입력_원문.md",
                 "cat docs/5_룰/5-8b_원천_JSON_형식.md", "mkdir -p \"$TEMP/t\" && cat > \"$TEMP/t/c.py\" <<'EOF'\nprint(open('입력_원문.md').read())\nEOF",
                 "PYTHONIOENCODING=utf-8 python -I \"$TEMP/t/c.py\" 입력_원문.md 추출_A.md"]
        잘못잡음 = {c: 명령_허용밖(c, 작A, "A") for c in 안명령 if 명령_허용밖(c, 작A, "A")}
        확인("Bash: 자기 작업 폴더 · 허용 문서 · $TEMP 자기 점검 스크립트는 허용", not 잘못잡음, 잘못잡음)

        # 실제 꼴: 예비 1_예비 추출자 A 로그의 Bash 명령 5개(.claude/작업/api_agents/runs/예비_1_예비_A.jsonl, 글자 그대로)
        실제작업 = 저장소 / "ontology" / "추출" / "예비" / "1_예비" / "작업_A"
        실제로그 = 임시 / "실제A.jsonl"
        _로그_쓰기(실제로그, "세션-실제", [("Read", {"file_path": str(저장소 / x)}) for x in 허용_문서[:4]]
                 + [("Read", {"file_path": str(실제작업 / x)}) for x in 입력파일] + [("Bash", {"command": c}) for c in _실제_A_명령])
        g = 로그_읽기(실제로그, 실제작업, "A")
        확인("실제 꼴(예비_1_예비_A 의 Bash 5개 · Read 7개)은 허용밖읽기 없음", g["허용밖읽기"] == [] and g["작업폴더_Read"] == 3, g["허용밖읽기"])
        진짜로그 = 저장소 / ".claude" / "작업" / "api_agents" / "runs" / "예비_1_예비_A.jsonl"
        if 진짜로그.is_file():          # .claude/ 는 git 밖이라 CI 에는 없다
            g = 로그_읽기(진짜로그, 실제작업, "A")
            확인("실제 로그 파일 예비_1_예비_A.jsonl 은 허용밖읽기 없음 · 세션 하나", g["허용밖읽기"] == [] and isinstance(g["에이전트ID"], str)
                 and g["에이전트ID"] != 미확인 and g["작업폴더_Read"] > 0, g["허용밖읽기"])

        # 마감 실패 사례: 모두 옮기지 않는다. 사례마다 묶음 복사본에서 돌려(잘못 통과해도 다음 사례에 번지지 않게) 결과만 본다
        for X in 추출자들:
            (묶음 / f"작업_{X}" / f"추출_{X}.md").write_text(f"### 조:시험#제1조\n추출 {X}\n", encoding="utf-8")

        def 깨끗(b: Path) -> list[tuple[str, dict]]:
            return [("Read", {"file_path": str(저장소 / 허용_문서[0])}), ("Read", {"file_path": str(b / "작업_A" / "입력_원문.md")}),
                    ("Bash", {"command": "ls"})]

        def 더러운(b: Path, 경로: Path) -> Path:
            _시험_로그(경로, b / "작업_A", b)
            return 경로
        로그들 = {
            "Read 허용 밖(재추출_대상 · 원천 JSON · 저장소 검색)": (lambda b, p: 더러운(b, p), "허용 밖 읽기 3건"),
            "Bash 허용 밖": (lambda b, p: _로그_쓰기(p, "세션-2", 깨끗(b) + [
                ("Bash", {"command": f"cat '{b.as_posix()}/재추출_대상.md' ../작업_B/추출_B.md"})]), "허용 밖 읽기"),
            "세션 둘(같은 이름으로 다시 돌린 로그)": (lambda b, p: _로그_쓰기(p, "세션-3", 깨끗(b), 둘째세션="세션-4"), "세션 ID 가 둘 이상"),
            "A 자리에 B 로그": (lambda b, p: _로그_쓰기(p, "세션-5", [("Read", {"file_path": str(b / "작업_B" / "입력_원문.md")})]),
                             "Read 경로가 작업_A 를 가리키지 않는다"),
        }
        for k, (이름, (만들기, 기대)) in enumerate(로그들.items()):
            b = 임시 / f"마감사례{k}"
            shutil.copytree(묶음, b)
            통과, 기록 = 마감(b, 로그={"A": str(만들기(b, 임시 / f"사례{k}.jsonl"))}, 에이전트={"B": "agent-b,claude-opus-5-5,high"})
            문제 = 기록["추출자"]["A"]["문제"]
            확인(f"마감 실패: {이름} · 옮기지 않음", not 통과 and any(기대 in m for m in 문제) and (b / "작업_A" / "추출_A.md").exists()
                 and not (b / "추출_A.md").exists(), 문제)

        깨끗로그 = _로그_쓰기(임시 / "깨끗.jsonl", "세션-6", 깨끗(묶음))
        (묶음 / "작업_B" / "메모.md").write_text("x", encoding="utf-8")
        통과, 기록 = 마감(묶음, 로그={"A": str(깨끗로그)}, 에이전트={"B": "agent-b,claude-opus-5-5,high"})
        확인("작업 폴더에 추가 파일이 있으면 마감 실패 · 추가파일 기록 · 옮기지 않음",
             not 통과 and 기록["추출자"]["B"]["추가파일"] == ["메모.md"] and (묶음 / "작업_A" / "추출_A.md").exists()
             and not (묶음 / "추출_A.md").exists() and (묶음 / "실행기록.json").is_file(), 기록["추출자"]["B"])
        (묶음 / "작업_B" / "메모.md").unlink()

        # 마감 실패: 작업 폴더 입력이 묶음 폴더 입력과 다르다(해시 대조). 묶음 복사본에서 돌린다
        b = 임시 / "마감사례_해시"
        shutil.copytree(묶음, b)
        (b / "작업_A" / "입력_원문.md").write_text("바뀐 본문\n", encoding="utf-8")
        통과, 기록 = 마감(b, 로그={"A": str(_로그_쓰기(임시 / "해시.jsonl", "세션-7", 깨끗(b)))}, 에이전트={"B": "agent-b,claude-opus-5-5,high"})
        확인("마감 실패: 작업 폴더 입력이 묶음 입력과 해시가 다름 · 옮기지 않음",
             not 통과 and "입력이 묶음 폴더의 것과 다르다: 입력_원문.md" in 기록["추출자"]["A"]["문제"]
             and not (b / "추출_A.md").exists(), 기록["추출자"]["A"]["문제"])

        # 마감: 정상
        통과, 기록 = 마감(묶음, 로그={"A": str(깨끗로그)}, 에이전트={"B": "agent-b,claude-opus-5-5,high"}, effort={"A": "high"})
        A, B = 기록["추출자"]["A"], 기록["추출자"]["B"]
        확인("정상 마감은 추출 파일을 묶음 폴더로 옮긴다", 통과 and (묶음 / "추출_A.md").is_file() and (묶음 / "추출_B.md").is_file()
             and not (묶음 / "작업_A" / "추출_A.md").exists(), [A["문제"], B["문제"]])
        for X, 행 in 기록["추출자"].items():
            글 = (묶음 / f"프롬프트_{X}.md").read_text(encoding="utf-8")
            앞, 뒤 = 덧붙인_문장(묶음 / f"작업_{X}", X)
            확인(f"덧붙인 문장({X})이 기록과 같고 앞 + 본문 + 뒤 = 프롬프트, 해시 일치",
                 행["덧붙인문장"] == {"앞": 앞, "뒤": 뒤} and 글 == f"{앞}\n\n{본문}\n\n{뒤}\n" and 행["프롬프트해시"] == _글해시(글), 행["덧붙인문장"])
        확인("기록 칸: 경로 · effort(출처 인자) · 프롬프트해시 출처 · 입력 폴더 파일 목록(sha256)",
             A["경로"] == "API" and A["effort"] == "high" and A["effort_출처"] == "인자" and B["effort_출처"] == "인자"
             and A["프롬프트해시_출처"] == "파일" and B["경로"] == "구독" and B["에이전트ID"] == "agent-b"
             and [f["이름"] for f in A["입력폴더"]] == sorted([*입력파일, "추출_A.md"])
             and all(len(f["sha256"]) == 64 for f in A["입력폴더"]), [A["입력폴더"], B])
        확인("실행기록.json 이 파일로 남는다", json.loads((묶음 / "실행기록.json").read_text(encoding="utf-8"))["통과"] is True)
        try:
            마감(묶음, 로그={"A": str(로그A)}, 에이전트={"B": "agent-b,claude-opus-5-5,high"})
            확인("이미 마감한 묶음을 다시 마감하면 실패 · 기록을 덮어쓰지 않음", False, "멈추지 않음")
        except 실행오류:
            확인("이미 마감한 묶음을 다시 마감하면 실패 · 기록을 덮어쓰지 않음",
                 json.loads((묶음 / "실행기록.json").read_text(encoding="utf-8"))["통과"] is True)

    실패 = [x for x in 결과 if not x[1]]
    for 이름, 참, 보충 in 결과:
        print(f"{'통과' if 참 else '실패'}: {이름}{'' if 참 else f' → {보충}'}")
    print(f"selftest: {len(결과) - len(실패)}/{len(결과)} 통과")
    return 1 if 실패 else 0


# CLI ─────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv[:1] == ["--self-test"]:
        return _자체시험()
    ap = argparse.ArgumentParser(description="추출 묶음 작업 폴더 준비 · 입력 폴더 검사 · 실행기록")
    sub = ap.add_subparsers(dest="명령", required=True)
    p = sub.add_parser("준비")
    p.add_argument("묶음")
    p.add_argument("--사업폴더키", required=True, help="run.py 작업 이름 · 로그 파일 이름의 앞머리")
    p.add_argument("--상한", type=float, default=40.0, help="run.py 작업 하나의 비용 상한(달러)")
    p = sub.add_parser("검사")
    p.add_argument("작업폴더")
    p = sub.add_parser("마감")
    p.add_argument("묶음")
    p.add_argument("--로그", action="append", help="X=<stream-json 로그>(API 경로)")
    p.add_argument("--에이전트", action="append", help="X=<ID>,<모델>,<effort>[,<시작>,<끝>](구독 경로 하위 에이전트)")
    p.add_argument("--effort", action="append", help="X=<effort>. 로그에 없을 때 호출 인자 값")
    a = ap.parse_args(argv)
    try:
        if a.명령 == "준비":
            r = 준비(Path(a.묶음), a.사업폴더키, a.상한)
            for x in r["작업"]:
                print(f"{x['이름']} | 프롬프트 {_경로표시(Path(x['프롬프트']))} | 작업 폴더 {_경로표시(Path(x['입력검사']))}")
            print(f"작업.json: {_경로표시(Path(a.묶음) / '작업.json')}")
            return 0
        if a.명령 == "검사":
            어긋남 = 입력폴더_검사(Path(a.작업폴더))
            for x in 어긋남:
                print(f"어긋남: {x}")
            print("입력 폴더 검사 통과" if not 어긋남 else f"입력 폴더 검사 실패: {len(어긋남)}건")
            return 1 if 어긋남 else 0
        통과, 기록 = 마감(Path(a.묶음), _짝(a.로그, "--로그"), _짝(a.에이전트, "--에이전트"), _짝(a.effort, "--effort"))
        for X, 행 in 기록["추출자"].items():
            print(f"{X} | {행['경로']} | {행['에이전트ID']} | {행['모델']} | effort {행['effort']}({행.get('effort_출처')}) | 허용밖읽기 "
                  f"{len(행['허용밖읽기']) if isinstance(행['허용밖읽기'], list) else 행['허용밖읽기']}")
            for m in 행["문제"]:
                print(f"  문제: {m}")
        print("마감 통과: 추출 파일을 묶음 폴더로 옮겼다" if 통과 else "마감 실패: 옮기지 않았다(실행기록.json 에 적음)")
        return 0 if 통과 else 1
    except 실행오류 as e:
        print(f"오류: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
