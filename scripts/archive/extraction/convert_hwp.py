# -*- coding: utf-8 -*-
"""HWP/HWPX → PDF 일괄 변환 (한컴 Office COM).

`구현.md` §1-5 가 "실시간 HWP 파싱"을 기각하고 한컴 1회 변환으로 확정한 그 작업이다.
파이썬 HWP 파서(hwp5txt)도 써봤으나 **표가 `<표>` 플레이스홀더로 전부 날아간다**
(예비창업 세부관리기준 실측: 표 17개 손실, 13,683자). 한컴 변환 PDF 는 21,571자에
표 8개가 실제로 살아 있다. 세부관리기준은 한도·증빙이 표에 있어서 표 손실은 치명적이다.

원본 트리 구조를 그대로 `_hwp변환/` 아래에 미러링한다.

실행:
    python scripts/archive/extraction/convert_hwp.py --dry-run
    python scripts/archive/extraction/convert_hwp.py
"""
from __future__ import annotations

# 🔴 2026-09-05 scripts/archive/ 이관 — 원래 scripts/ 바로 밑에 있던 파일이라
#    아래(또는 이 파일의 기존 sys.path 계산)는 scripts/ 바로 밑 기준으로 짜여 있다.
#    이관으로 깊이가 늘어나 깨지므로, `scripts/_lib` 을 찾을 때까지 위로 걸어 올라가
#    scripts/ 와 프로젝트 루트를 sys.path 맨 앞에 다시 건다.
import os as _os_이관, sys as _sys_이관
_p_이관 = _os_이관.path.dirname(_os_이관.path.abspath(__file__))
while not _os_이관.path.isdir(_os_이관.path.join(_p_이관, "_lib")):
    _parent_이관 = _os_이관.path.dirname(_p_이관)
    if _parent_이관 == _p_이관:
        break
    _p_이관 = _parent_이관
if _p_이관 not in _sys_이관.path:
    _sys_이관.path.insert(0, _p_이관)
if _os_이관.path.dirname(_p_이관) not in _sys_이관.path:
    _sys_이관.path.insert(0, _os_이관.path.dirname(_p_이관))
# 🔴 archive 내부에서 카테고리를 넘나드는 import(예: index_guard, stage0_run)가
#    있어 scripts/archive/ 의 모든 하위 폴더도 같이 건다.
_archive_이관 = _os_이관.path.join(_p_이관, "archive")
if _os_이관.path.isdir(_archive_이관):
    for _d_이관 in _os_이관.listdir(_archive_이관):
        _full_이관 = _os_이관.path.join(_archive_이관, _d_이관)
        if _os_이관.path.isdir(_full_이관) and _full_이관 not in _sys_이관.path:
            _sys_이관.path.insert(0, _full_이관)


import argparse
import json
import os
import subprocess
import time
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "scripts" / "_lib").is_dir())  # 🔴 2026-09-05 archive 이관 — 깊이 무관 계산으로 교체
OUT = ROOT / "_hwp변환"
# 2026-08-27: 행정규칙 별표·첨부 추가. 별표는 「보조사업비 카드 사용제한 업종」처럼
# 비목 적격성에 직결되는 표라 변환 없이는 판정이 안 된다 (법령_크롤링_현황.md §5).
SCAN = [
    "2026_Finance_DATA_FOR_RAG",
    "건국대학교 레퍼런스",
    "법령 PDF/L1_법령/별표",
    "법령 PDF/L1_법령/첨부",
]


def sniff(path: Path) -> str | None:
    """매직바이트로 실제 포맷을 판별한다. 확장자는 신뢰하지 않는다.

    OLE(D0CF11E0) → HWP v5 / ZIP(PK) → HWPX / 그 외 → None(변환 불가)
    """
    with open(path, "rb") as fh:
        head = fh.read(8)
    if head[:8] == bytes.fromhex("d0cf11e0a1b11ae1"):
        return "HWP"
    if head[:2] == b"PK":
        return "HWPX"
    return None


def targets() -> list[Path]:
    out = []
    for base in SCAN:
        for ext in ("*.hwp", "*.hwpx"):
            # `_정리보류/` 는 중복·범위외로 격리한 파일이라 변환 대상이 아니다
            out += [f for f in sorted((ROOT / base).rglob(ext))
                    if "_정리보류" not in f.parts]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    files = targets()
    print("변환 대상 %d건" % len(files))
    if args.dry_run:
        for f in files:
            print("   ", f.relative_to(ROOT))
        return

    import win32com.client as win32
    hwp = win32.gencache.EnsureDispatch("HWPFrame.HwpObject")
    try:
        hwp.RegisterModule("FilePathCheckDLL", "FilePathCheckerModule")
    except Exception:                                            # noqa: BLE001
        print("보안모듈 미등록 — 대화상자가 뜰 수 있다")
    hwp.XHwpWindows.Item(0).Visible = False

    log, ok, fail, skip = [], 0, 0, 0
    for i, src in enumerate(files, 1):
        rel = src.relative_to(ROOT)
        dst = OUT / rel.parent / (src.stem + ".pdf")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and dst.stat().st_size > 1024:
            skip += 1
            continue
        t0 = time.time()
        try:
            # ⚠️ 확장자를 믿으면 안 된다 (2026-08-27 실측).
            #    law.go.kr 별표(admbyl)가 주는 파일은 확장자가 .hwp 인데 실제로는
            #    HWPX(zip, mimetype=application/hwp+zip)인 경우가 67건이었다.
            #    포맷을 틀리게 주면 Open 이 조용히 False 를 돌려준다.
            #    일부(5건)는 아예 HWP 가 아니라 HTML 오류페이지다 → 재수집 대상.
            fmt = sniff(src)
            if fmt is None:
                raise RuntimeError("HWP/HWPX 가 아님(HTML 오류페이지 등) — 재수집 필요")
            if not hwp.Open(str(src), fmt, "forceopen:true"):
                raise RuntimeError("Open 실패(format=%s)" % fmt)
            mode = hwp.EditMode          # 17 = 배포용 문서 → SaveAs 가 막힌다
            saved = hwp.SaveAs(str(dst), "PDF") if mode != 17 else False
            hwp.Clear(1)                      # 문서 닫기(저장 안 함)
            if not saved or not dst.exists() or dst.stat().st_size < 1024:
                # 배포용 문서는 한컴이 저장을 거부한다(EditMode 를 1 로 바꿔도 안 풀림).
                # hwp5txt 로 텍스트만 뽑는다 — 표는 <표> 플레이스홀더로 날아가지만
                # 인용 법령 추출에는 쓸 수 있다.
                txt = dst.with_suffix(".txt")
                r = subprocess.run(["hwp5txt", "--output", str(txt), str(src)],
                                   capture_output=True, timeout=180)
                if r.returncode != 0 or not txt.exists() or txt.stat().st_size < 200:
                    raise RuntimeError("PDF 저장 거부(EditMode=%s) + hwp5txt 실패" % mode)
                ok += 1
                print("[%2d/%d] TXT %5.1fs %7.1fKB  %s (배포용)"
                      % (i, len(files), time.time() - t0, txt.stat().st_size / 1024,
                         src.name[:48]), flush=True)
                log.append({"src": str(rel).replace("\\", "/"),
                            "txt": str(txt.relative_to(ROOT)).replace("\\", "/"),
                            "bytes": txt.stat().st_size, "status": "ok",
                            "method": "hwp5txt", "editmode": mode,
                            "note": "배포용 문서 — 표 손실"})
                continue
            ok += 1
            print("[%2d/%d] OK %5.1fs %7.1fKB  %s"
                  % (i, len(files), time.time() - t0, dst.stat().st_size / 1024, src.name[:56]),
                  flush=True)
            log.append({"src": str(rel).replace("\\", "/"),
                        "pdf": str(dst.relative_to(ROOT)).replace("\\", "/"),
                        "bytes": dst.stat().st_size, "status": "ok",
                        "method": "hancom-pdf", "editmode": mode})
        except Exception as e:                                   # noqa: BLE001
            fail += 1
            print("[%2d/%d] FAIL %s <- %s" % (i, len(files), src.name[:46], str(e)[:50]),
                  flush=True)
            log.append({"src": str(rel).replace("\\", "/"), "status": "fail",
                        "error": str(e)[:200]})
    try:
        hwp.Quit()
    except Exception:                                            # noqa: BLE001
        pass

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "_변환로그.json").write_text(json.dumps(log, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    print("\n성공 %d / 실패 %d / 이미 있음 %d" % (ok, fail, skip))


if __name__ == "__main__":
    main()
