# -*- coding: utf-8 -*-
"""SessionStart — 손으로 갱신하던 "현재 위치"를 실측값으로 대체한다.

구 CLAUDE.md 는 `현재 위치 (2026-08-27)` 를 본문에 박아뒀는데, 사람이 고쳐야 해서
하루 만에 낡았다. 여기서 매 세션 실제 파일·git 을 읽어 뱉는다.
stdout 은 세션 컨텍스트에 주입된다. 어떤 경우에도 세션을 막지 않는다.
"""
import io
import subprocess
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[2]


def worklog_head(n: int = 3) -> list[str]:
    """최근 작업 일자와 그 날의 첫 항목들.

    기록은 `docs/기록/YYYY-MM-DD.md` 로 날짜당 한 벌이다 (2026-09-02 분할).
    분할 전 저장소에서도 돌게 `작업 현황.md` 를 대체 경로로 남겨둔다.
    """
    d = ROOT / "docs" / "기록"
    if d.is_dir():
        # `2026-09-01.md` 만. `2026-08-31_축별보고.md` 는 일자 기록이 아니다
        일자 = sorted((p for p in d.glob("20??-??-??.md")), reverse=True)
        out = []
        for f in 일자[:n]:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("## "):
                    제목 = line[3:].strip()
                    # 절 제목이 이미 날짜로 시작하면 파일명과 겹친다
                    if 제목.startswith(f.stem):
                        제목 = 제목[len(f.stem):].lstrip(" ·-—")
                    out.append(f"{f.stem} · {제목}" if 제목 else f.stem)
                    break
        if out:
            return out

    f = ROOT / "작업 현황.md"
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("## 20"):
            out.append(line[3:].strip())
            if len(out) >= n:
                break
    return out


def count(rel: str, *patterns: str, deep: bool = True) -> int:
    d = ROOT / rel
    if not d.is_dir():
        return 0
    it = d.rglob if deep else d.glob
    return sum(len(list(it(pat))) for pat in patterns)


def git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        ).stdout.strip()
    except Exception:
        return ""


def main() -> None:
    print("## 세션 시작 실측 (SessionStart 훅 — CLAUDE.md 의 숫자보다 이쪽이 최신)")

    log = worklog_head()
    if log:
        print("\n### 최근 작업 기록")
        for line in log:
            print(f"- {line}")

    print("\n### 코퍼스 실측")
    DOC = ("*.pdf", "*.hwp", "*.hwpx", "*.xml", "*.txt")
    rows = [
        ("법령·행정규칙 XML 현행", count("법령 PDF/L1_법령", "*.xml", deep=False)),
        ("  └ 구판(연혁)", count("법령 PDF/L1_법령/연혁", "*.xml")),
        ("  └ 행정규칙 별표 HWP", count("법령 PDF/L1_법령/별표", "*.hwp", "*.hwpx")),
        ("HWP→PDF 변환 산출", count("_hwp변환", "*.pdf")),
        ("  └ 그중 별표", count("_hwp변환/법령 PDF/L1_법령/별표", "*.pdf")),
        ("신규 데이터셋 문서", count("2026_Finance_DATA_FOR_RAG", *DOC)),
    ]
    for name, n in rows:
        print(f"- {name}: {n}")

    st = git("status", "--porcelain")
    if st:
        print(f"\n### git — 미커밋 {len(st.splitlines())}건 (브랜치 {git('branch', '--show-current')})")

    print("\n읽기 순서는 `docs/README.md`, 지금 상태는 `docs/0_현황.md`, "
          "미결은 `docs/9_미결.md`, 점검 절차는 `docs/8_운영/8-2_점검_절차.md`.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
