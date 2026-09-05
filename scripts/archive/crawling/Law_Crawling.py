# -*- coding: utf-8 -*-
"""국가법령정보 OPEN API 수집기 — 「써도돼요」 L1 법령·행정규칙.

입력은 `법령 연계 모음/문서_법령_링크모음.md` §1 마스터 목록이다.
그 표를 직접 읽어 대상(L01~L39, R01~R12)과 참조 조항을 뽑고,
각 규범의 현행본 + 문서 연도별 시점본을 DRF XML로 내려받는다.

XML로 받는 이유: scripts/stage0_extract.py::extract_xml() 이 이미 이 포맷을
조 단위로 파싱한다. JSON을 쓰면 동등한 파서를 새로 짜야 하고, 단일 원소가
배열이 아닌 객체로 오는 함정까지 따로 처리해야 한다.

실행 (환경변수 LAW_GO_KR_OC 에 law.go.kr 신청 ID 필요):
    python Law_Crawling.py --healthcheck     OC 키 동작 확인만
    python Law_Crawling.py --list            md 파싱 결과만 확인 (호출 없음)
    python Law_Crawling.py                   전체 수집 (현행 + 시점본)
    python Law_Crawling.py --no-history      현행본만
    python Law_Crawling.py --only L09,R01    특정 항목만
    python Law_Crawling.py --report          기존 리포트 재출력
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
import io
import json
import os
import re
import sys
import time
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

# stdout 래핑은 main() 에서만 한다.
# 모듈 로드 시점에 갈아끼우면 이 모듈을 import 하는 쪽의 stdout 이 닫힌다
# (scripts/build_citations.py 가 parse_articles 를 재사용한다).

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "scripts" / "_lib").is_dir())  # 🔴 2026-09-05 archive 이관 — 깊이 무관 계산으로 교체
MD_PATH = ROOT / "법령 연계 모음" / "문서_법령_링크모음.md"
OUT_DIR = ROOT / "법령 PDF" / "L1_법령"
HIST_DIR = OUT_DIR / "연혁"
CACHE_PATH = ROOT / "법령 PDF" / "_law_cache.json"
REPORT_PATH = ROOT / "법령 PDF" / "_law_report.json"
DROP_PATH = ROOT / "법령 PDF" / "_law_delegated_dropped.json"

# law.go.kr OPEN API 신청 ID. 공개 저장소에 개인 ID를 남기지 않으려고 환경변수로 뺐다.
#   PowerShell:  $env:LAW_GO_KR_OC = "<신청ID>"
#   bash:        export LAW_GO_KR_OC='<신청ID>'
# 비어 있으면 Api() 생성 시점에 죽는다 — OC 가 틀리면 API 가 HTTP 200 에
# 빈 결과를 돌려주기 때문에(§12 함정) 조용한 0건으로 새는 걸 막아야 한다.
OC = os.environ.get("LAW_GO_KR_OC", "")
BASE = "https://www.law.go.kr/DRF"
TIMEOUT = 30
SLEEP_SEC = 0.7
MAX_RETRY = 3
DOC_YEARS = (2022, 2023, 2024, 2025, 2026)   # 세부관리기준 연도판
# 2026-08-24: 신규 데이터셋(2026_finance_data_for_RAG/창진원)에 2022년판
# 「창업도약패키지 세부관리기준(2022년)」이 있어 2022 를 추가했다.

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
}

# 문서 표기 → law.go.kr 현행 제명. md §2 의 '구 법명' 항목.
ALIAS = {
    "L19": "공무원교육훈련법",   # 2016.1.1 「공무원 인재개발법」으로 전부개정
}

# md 는 '△ 2009년도판만 등재'라 적었으나 실제 검색은 0건이다(2026-08 확인).
# 0건 항목으로 두면 '별칭 매핑 누락'으로 오독되므로 수동수집으로 분류한다.
KNOWN_ZERO = {"R08"}

# md 가 '×' 로 표시한 항목의 확보 경로 (재시도 루프 금지)
UNAVAILABLE_REASON = {
    "R05": "창업진흥원 운영지침. 국가법령정보센터 미등재 → k-startup / vcs.go.kr 수동 확보",
    "R06": "R05의 2025.12 개명판(제14차). 미등재 → vcs.go.kr 수동 확보",
    "R07": "소상공인시장진흥공단 내부 운영지침 → 소진공 공고 별첨",
    "R08": "law.go.kr 검색 0건(2026-08 확인). 기재부 열린재정에서 연도판 확보 필요",
    "R11": "한국엔젤투자협회 TIPS 운영사무국 배포 → jointips.or.kr",
    "R12": "기관 내규",
}

# 문서 인용이 실제 조문과 어긋나 보강이 필요한 경우 (md §2 '조문 부적합 가능성')
AUGMENT = {
    "L09": (["777"],
            "문서는 제767조(친족의 정의)를 인용하나 친족 범위 실체 규정은 제777조"),
    # 「공무원 여비 규정」 별표 1 은 '여비 지급 구분표'(공무원 등급 분류)다.
    # 문서가 준용하는 숙박비 실액은 별표 2(국내), 국외 여비는 별표 4 에 있다.
    "L08": (["별표2", "별표4"],
            "별표 1 은 등급 구분표일 뿐 금액이 없다. 숙박비 실액은 별표 2(국내 여비 지급표), "
            "국외 여비는 별표 4(국외 여비 지급표)"),
}


def norm(s: str) -> str:
    """법령명 비교용 정규화 — 공백 제거."""
    return re.sub(r"\s+", "", s or "")


def safe_name(s: str) -> str:
    """파일명 정제. 기존 L1_* 규칙(공백 제거)을 따른다."""
    return re.sub(r'[\\/:*?"<>|]', "", re.sub(r"\s+", "", s or ""))


# ── 참조 조항 파싱 ────────────────────────────────────────────────
def parse_articles(s: str) -> tuple[list[str], list[str]]:
    """md 의 참조 조항 문자열 → 조 식별자 리스트.

    '35' = 제35조 / '31-2' = 제31조의2 / '별표1' = 별표 1
    md §2-5 가 열거한 표기가 전부 등장한다:
        제35조 / 제31조의2 / 제2조제6호 / 제21조제1항제1호나목
        제13조③ / 제33조~제42조 / 제24조 내지 제42조 / 제51조·제52조
        제8·9·17~19조 (조가 맨 뒤에만 붙는 축약 열거) / 별표 1 제2호
    """
    if not s:
        return [], []
    out: list[str] = []
    notes: list[str] = []

    if "판독 불가" in s or "제1*조" in s:
        notes.append("판독불가")

    for m in re.finditer(r"별표\s*(\d+)", s):
        out.append(f"별표{m.group(1)}")

    for m in re.finditer(r"제\s*(\d+)\s*조(?:\s*의\s*(\d+))?", s):
        jo, br = m.group(1), m.group(2)
        out.append(f"{jo}-{br}" if br else jo)

    # 범위 전개: 제A조~제B조 / 제A조 내지 제B조
    for m in re.finditer(r"제\s*(\d+)\s*조\s*(?:~|～|-|내지)\s*제?\s*(\d+)\s*조", s):
        a, b = int(m.group(1)), int(m.group(2))
        if 0 < b - a <= 60:
            out.extend(str(x) for x in range(a, b + 1))
            notes.append(f"범위전개 제{a}조~제{b}조")

    # 축약 열거: 제8·9·17~19·21~32조
    for m in re.finditer(r"제((?:\d+(?:\s*[~～]\s*\d+)?[·,]\s*)+\d+(?:\s*[~～]\s*\d+)?)\s*조", s):
        for part in re.split(r"[·,]", m.group(1)):
            part = part.strip()
            if re.fullmatch(r"\d+", part):
                out.append(part)
                continue
            rg = re.fullmatch(r"(\d+)\s*[~～]\s*(\d+)", part)
            if rg:
                a, b = int(rg.group(1)), int(rg.group(2))
                if 0 < b - a <= 60:
                    out.extend(str(x) for x in range(a, b + 1))

    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq, notes


# ── 마스터 목록 로드 ──────────────────────────────────────────────
def load_master(md_path: Path = MD_PATH) -> list[dict]:
    """문서_법령_링크모음.md §1-A(법령)·§1-B(행정규칙) 표를 읽는다.

    열 구성: | # | 검색키워드 | 종류 | 문서 내 표기 | 참조 조항 | law.go.kr | 직링크 |
    law.go.kr 열이 '×' 면 API 미등재 → target=None (수동수집).
    """
    if not md_path.exists():
        raise FileNotFoundError(f"마스터 목록을 찾을 수 없습니다: {md_path}")

    section = None
    rows: list[dict] = []
    for line in md_path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("### 1-A"):
            section = "law"
            continue
        if s.startswith("### 1-B"):
            section = "admrul"
            continue
        if s.startswith("### 1-C") or s.startswith("## §2"):
            section = None
            continue
        if not section or not s.startswith("|"):
            continue

        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 6 or not re.fullmatch(r"[LR]\d{2}", cells[0]):
            continue

        ref_id, keyword, doc_type = cells[0], cells[1], cells[2]
        as_written, cited_raw, availability = cells[3], cells[4], cells[5]
        available = "×" not in availability and ref_id not in KNOWN_ZERO
        cited, notes = parse_articles(cited_raw)

        if ref_id in AUGMENT:
            extras, why = AUGMENT[ref_id]
            added = [e for e in extras if e not in cited]
            cited.extend(added)
            if added:
                notes.append(f"보강수집 {', '.join(added)} — {why}")

        rows.append({
            "ref_id": ref_id,
            "keyword": keyword,
            "doc_type": doc_type,
            "as_written": as_written,
            "target": section if available else None,
            "cited": cited,
            "cited_raw": cited_raw,
            "parse_notes": notes,
        })
    return rows


# ── HTTP ─────────────────────────────────────────────────────────
def require_oc() -> str:
    """OC 없이 부르면 여기서 죽인다.

    law.go.kr 은 OC 가 비었거나 틀려도 HTTP 200 에 빈 결과를 준다(§12 함정).
    그대로 두면 파싱이 조용히 0건으로 끝나 수집 실패를 못 알아챈다.
    """
    if not OC:
        raise SystemExit(
            "환경변수 LAW_GO_KR_OC 가 비어 있다. law.go.kr OPEN API 신청 ID를 넣어라." \
            + "\n  PowerShell:  $env:LAW_GO_KR_OC = '<신청ID>'" \
            + "\n  bash:        export LAW_GO_KR_OC='<신청ID>'"
        )
    return OC


class Api:
    def __init__(self, oc: str = ""):
        self.oc = oc or require_oc()
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.calls = 0

    def _get(self, path: str, params: dict) -> requests.Response:
        p = {"OC": self.oc}
        p.update(params)
        last = None
        for attempt in range(1, MAX_RETRY + 1):
            try:
                r = self.s.get(f"{BASE}/{path}", params=p, timeout=TIMEOUT)
                r.raise_for_status()
                r.encoding = "utf-8"
                self.calls += 1
                return r
            except requests.RequestException as e:
                last = e
                wait = 2 * attempt
                print(f"    [retry {attempt}/{MAX_RETRY}] {e} → {wait}초 후", file=sys.stderr)
                time.sleep(wait)
        raise RuntimeError(f"{path} {params} 호출 실패") from last

    def search(self, target: str, query: str, **extra) -> list[dict]:
        params = {"target": target, "type": "XML", "query": query, "display": "100"}
        params.update(extra)
        r = self._get("lawSearch.do", params)
        if not r.text.lstrip().startswith("<"):
            return []
        try:
            root = ET.fromstring(r.text)
        except ET.ParseError:
            return []
        tag = "law" if target in ("law", "eflaw") else "admrul"
        return [{c.tag: (c.text or "").strip() for c in node} for node in root.findall(tag)]

    def body_xml(self, target: str, **key) -> str:
        """본문 XML 원문. 법령은 MST=, 행정규칙은 ID= 를 쓴다."""
        params = {"target": target, "type": "XML"}
        params.update(key)
        return self._get("lawService.do", params).text


# ── 해결(resolve) ────────────────────────────────────────────────
def resolve_law(api: Api, keyword: str, alias: str | None = None) -> dict | None:
    """검색 → 공백제거 완전일치 + 현행 필터.

    검색이 매우 느슨하다 (query=상법 → 56건, 무관한 법령 다수).
    완전일치 필터가 없으면 엉뚱한 법을 집는다.
    """
    want = norm(keyword)
    for q in filter(None, [keyword, alias]):
        items = api.search("law", q)
        for it in items:
            if norm(it.get("법령명한글")) == want and it.get("현행연혁코드") == "현행":
                return it
        for it in items:      # 약칭 매칭
            if norm(it.get("법령약칭명")) == want and it.get("현행연혁코드") == "현행":
                return it
    return None


def _sim(a: str, b: str) -> float:
    """제명 유사도 — 문자 집합 자카드. 제명 개정 추적용."""
    sa, sb = set(a), set(b)
    return len(sa & sb) / max(1, len(sa | sb))


def resolve_admrul(api: Api, keyword: str) -> dict | None:
    """행정규칙 현행본 해결.

    제명이 개정으로 바뀌는 일이 잦다. 실측: 위임 정보가 가리킨
    「창업 및 창업기업 범위에 관한 규정」(2022-46) 은 폐지되고 현행은
    「창업기업 및 국외 창업기업 범위에 관한 규정」(2025-138) 이다.
    두 판의 행정규칙ID 는 82325 로 같다 — ID 가 개정을 관통하는 안정 키다.
    law.go.kr 검색이 구 제명으로도 현행을 찾아주므로, 완전일치가 실패하면
    유사도로 고른다.
    """
    want = norm(keyword)
    items = api.search("admrul", keyword)
    if not items:
        return None

    exact = [x for x in items if norm(x.get("행정규칙명")) == want]
    cur = [x for x in exact if x.get("현행연혁구분") == "현행"]
    if cur:
        return cur[0]
    if exact:
        return exact[0]

    # 제명 개정 — 현행 후보 중 가장 비슷한 것
    cands = [x for x in items if x.get("현행연혁구분") == "현행"]
    if not cands:
        return None
    best = max(cands, key=lambda x: _sim(want, norm(x.get("행정규칙명"))))
    return best if _sim(want, norm(best.get("행정규칙명"))) >= 0.6 else None


def historical_msts(api: Api, keyword: str, years=DOC_YEARS) -> list[dict]:
    """target=eflaw + efYd 로 연도별 시행 버전을 잡는다.

    target=law + efYd 는 0건이 나온다. eflaw 를 써야 한다.
    """
    want = norm(keyword)
    seen, out = set(), []
    for y in years:
        for it in api.search("eflaw", keyword, efYd=f"{y}0101~{y}1231"):
            if norm(it.get("법령명한글")) != want:
                continue
            mst = it.get("법령일련번호")
            if mst and mst not in seen:
                seen.add(mst)
                out.append({**it, "_year": y})
        time.sleep(SLEEP_SEC)
    return out


# ── 수집 ─────────────────────────────────────────────────────────
_RE_ADM_JO = re.compile(r"^제\s*(\d+)\s*조(?:\s*의\s*(\d+))?\s*(?:\(([^)]*)\))?")


def _admrul_index(root: ET.Element) -> dict[str, str]:
    """행정규칙은 <조문단위>가 없다. 평문 <조문내용>에서 조번호→제목을 만든다."""
    idx = {}
    for n in root.findall(".//조문내용"):
        m = _RE_ADM_JO.match((n.text or "").strip())
        if not m:
            continue
        key = f"{m.group(1)}-{m.group(2)}" if m.group(2) else m.group(1)
        idx.setdefault(key, (m.group(3) or "").strip() or "(제목없음)")
    return idx


def article_titles(xml_text: str, wanted: list[str]) -> list[dict]:
    """리포트용 — 인용 조문의 조문제목을 뽑아 오인용을 사람이 잡게 한다."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    adm = _admrul_index(root) if not root.findall(".//조문단위") else {}
    found = []
    for w in wanted:
        if w.startswith("별표"):
            n = w[2:].strip()
            hit = None
            for b in root.findall(".//별표단위"):
                if (b.findtext("별표번호") or "").lstrip("0") == n:
                    hit = (b.findtext("별표제목") or "").strip()
                    break
            found.append({"ref": f"별표 {n}", "title": hit, "ok": hit is not None})
            continue

        jo, _, branch = w.partition("-")
        label = f"제{jo}조" + (f"의{branch}" if branch else "")
        if adm:
            t = adm.get(w)
            found.append({"ref": label, "title": t, "ok": t is not None})
            continue
        hit = None
        for u in root.findall(".//조문단위"):
            if (u.findtext("조문여부") or "").strip() != "조문":
                continue
            if (u.findtext("조문번호") or "").strip() != jo:
                continue
            if branch and (u.findtext("조문가지번호") or "").strip() != branch:
                continue
            if not branch and (u.findtext("조문가지번호") or "").strip():
                continue
            hit = (u.findtext("조문제목") or "").strip() or "(제목없음)"
            break
        found.append({"ref": label, "title": hit, "ok": hit is not None})
    return found


def save_xml(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def collect(api: Api, master: list[dict], only: set[str] | None, with_history: bool) -> list[dict]:
    cache = json.loads(CACHE_PATH.read_text(encoding="utf-8")) if CACHE_PATH.exists() else {}
    results = []

    for item in master:
        ref_id, keyword, target = item["ref_id"], item["keyword"], item["target"]
        if only and ref_id not in only:
            continue

        rec = dict(item)
        rec.update({"flags": list(item["parse_notes"]), "files": [], "article_titles": []})

        if target is None:
            rec["status"] = "수동수집"
            rec["flags"].append("수동수집")
            rec["reason"] = UNAVAILABLE_REASON.get(ref_id, "md 에서 '×' 표시")
            print(f"[{ref_id}] {keyword} — 수동수집 (API 미등재)")
            results.append(rec)
            continue

        alias = ALIAS.get(ref_id)
        print(f"[{ref_id}] {keyword}", end="", flush=True)

        try:
            hit = (resolve_law(api, keyword, alias) if target == "law"
                   else resolve_admrul(api, keyword))
        except RuntimeError as e:
            rec.update({"status": "오류", "reason": str(e)})
            rec["flags"].append("호출실패")
            print("  → 호출 실패")
            results.append(rec)
            continue

        if not hit:
            rec["status"] = "0건"
            rec["flags"].append("0건")
            print("  → 0건")
            results.append(rec)
            continue

        if alias:
            rec["flags"].append("구법명")

        if target == "law":
            name, eff = hit.get("법령명한글"), hit.get("시행일자")
            rec.update({"law_id": hit.get("법령ID"), "mst": hit.get("법령일련번호"),
                        "name": name, "effective_date": eff,
                        "promulgation_no": hit.get("공포번호"),
                        "law_type": hit.get("법령구분명"), "ministry": hit.get("소관부처명")})
            body = api.body_xml("law", MST=hit["법령일련번호"])
        else:
            name, eff = hit.get("행정규칙명"), hit.get("시행일자")
            rec.update({"law_id": hit.get("행정규칙ID"), "mst": hit.get("행정규칙일련번호"),
                        "name": name, "effective_date": eff,
                        "promulgation_no": hit.get("발령번호"),
                        "law_type": hit.get("행정규칙종류"), "ministry": hit.get("소관부처명")})
            body = api.body_xml("admrul", ID=hit["행정규칙일련번호"])
            if norm(name) != norm(keyword):
                rec["flags"].append("제명변경")

        fname = f"L1_{safe_name(name)}_{eff}.xml"
        save_xml(OUT_DIR / fname, body)
        rec["files"].append({"file": f"법령 PDF/L1_법령/{fname}", "kind": "현행",
                             "mst": rec["mst"], "effective_date": eff})
        rec["status"] = "수집"
        rec["article_titles"] = article_titles(body, item["cited"])
        if any(not t["ok"] for t in rec["article_titles"]):
            rec["flags"].append("조문없음")
        print(f"  → {name} (시행 {eff}, MST {rec['mst']})")
        time.sleep(SLEEP_SEC)

        # 시점본 — 법령만. 행정규칙은 eflaw 대상이 아니다.
        if with_history and target == "law":
            for h in historical_msts(api, name):
                mst = h["법령일련번호"]
                if mst == rec["mst"]:
                    continue
                heff = h.get("시행일자")
                # 같은 날 시행되는 개정이 여러 건일 수 있다(소득세법 시행령 등).
                # 시행일자만으로 이름을 지으면 덮어써지므로 MST 를 함께 넣는다.
                hname = f"L1_{safe_name(name)}_{heff}_{mst}.xml"
                hpath = HIST_DIR / hname
                if cache.get(f"hist:{mst}") and hpath.exists():
                    pass
                else:
                    save_xml(hpath, api.body_xml("law", MST=mst))
                    cache[f"hist:{mst}"] = heff
                    time.sleep(SLEEP_SEC)
                rec["files"].append({"file": f"법령 PDF/L1_법령/연혁/{hname}", "kind": "시점본",
                                     "mst": mst, "effective_date": heff,
                                     "doc_year": h.get("_year")})
                print(f"      연혁 {heff} (MST {mst}, {h.get('_year')}년 문서용)")

        results.append(rec)

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    return results


# ── 위임 추적 (하이퍼링크 따라가기) ────────────────────────────────
# 법률 → 시행령·시행규칙 → 위임 행정규칙 의 3단계.
# '인용법령'은 위임이 아니라 단순 참조다. 따라가면 「중소기업기본법」→「상법」→…
# 로 전체 법령까지 번지므로 반드시 제외한다.
DELEGATE_KINDS = {"시행령", "시행규칙", "위임행정규칙", "위임규정"}


_RE_JOHANG = re.compile(r"제\s*(\d+)\s*조(?:\s*의\s*(\d+))?")


def _jo_of(조항호목: str) -> str | None:
    """'제3조의2제3항' → '3-2' / '제2조제5항' → '2'."""
    m = _RE_JOHANG.match((조항호목 or "").strip())
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}" if m.group(2) else m.group(1)


_RE_SUB = re.compile(r"(시행령|시행규칙)$")


def _base_name(name: str) -> str:
    """'고등교육법 시행령' → '고등교육법'. 직접 하위 판정의 기준."""
    return _RE_SUB.sub("", norm(name))


def delegated_targets(api: Api, mst: str, cited: set[str],
                      parent: str = "") -> tuple[list, list]:
    """lsDelegated → (따라갈 대상, 범위 밖으로 제외한 대상).

    응답의 위임 일련번호는 구판을 가리킨다 (실측: 「중소기업창업 지원사업
    운영요령」이 2100000222594 = 구판, 현행은 2100000250454). 일련번호를
    그대로 쓰면 폐지된 규범을 인덱싱하므로 제목 → 현행 재조회로 간다.

    필터 정책
      제명이 부모 법령으로 시작하는 시행령·시행규칙
          → 항상 따라간다 (고등교육법 → 고등교육법 시행령. 진짜 직접 하위)
      그 밖의 모든 위임 (다른 법령·행정규칙)
          → 문서가 인용한 조가 위임한 것만 따라간다
      인용법령  → 위임이 아니라 단순 참조 → 항상 제외

    law.go.kr 은 **다른 법으로 넘어가는 위임도 위임구분=시행령/시행규칙 으로
    표시한다** (실측: 국민연금법 시행규칙 → 고용보험법 시행규칙,
    고등교육법 → 한국교원대학교 설치령). 위임구분만 믿고 따라가면 창업지원금과
    무관한 법령이 수백 건 딸려온다. 제명 접두 일치로 걸러야 한다.
    """
    try:
        root = ET.fromstring(api.body_xml("lsDelegated", MST=mst))
    except (RuntimeError, ET.ParseError):
        return [], []

    base = _base_name(parent)
    keep, drop, seen = [], [], set()
    for w in root.findall(".//위임정보"):
        kind = (w.findtext("위임구분") or "").strip()
        if kind not in DELEGATE_KINDS:
            continue

        if kind == "위임행정규칙":
            items = [((u.findtext("위임행정규칙제목") or "").strip(),
                      (u.findtext("조항호목") or "").strip())
                     for u in w.findall(".//위임행정규칙조문정보")]
        else:
            src = [(u.findtext("조항호목") or "").strip()
                   for u in w.findall(".//위임법령조문정보")] or [""]
            items = [((w.findtext("위임법령제목") or "").strip(), src[0])]

        for title, 조항 in items:
            if not title or (kind, title) in seen:
                continue
            seen.add((kind, title))
            # 제명이 부모로 시작하는 시행령·시행규칙 = 진짜 직접 하위
            direct = (kind in ("시행령", "시행규칙")
                      and base and norm(title).startswith(base))
            if not direct:
                jo = _jo_of(조항)
                if not cited or jo not in cited:
                    drop.append((kind, title, 조항))
                    continue
            keep.append((kind, title, 조항))
    return keep, drop


def crawl_delegated(api: Api, collected: list[dict],
                    max_depth: int = 2) -> tuple[list[dict], list[dict]]:
    """위임 계통을 따라 누락된 하위법령을 채운다.

    3단계:  법률(depth 0) → 시행령·시행규칙(depth 1) → 위임 행정규칙(depth 2)
    """
    have_names = {norm(r["name"]) for r in collected if r.get("name")}
    have_ids = {r.get("law_id") for r in collected if r.get("law_id")}
    # 문서가 인용한 조 — 위임 행정규칙 필터의 기준
    cited_by_name = {norm(r["name"]): set(r.get("cited") or [])
                     for r in collected if r.get("name")}
    extra: list[dict] = []
    dropped: list[dict] = []

    queue = [(r["name"], r["mst"], 0) for r in collected
             if r.get("target") == "law" and r.get("mst")]
    visited_mst = set()

    while queue:
        name, mst, depth = queue.pop(0)
        if depth >= max_depth or mst in visited_mst:
            continue
        visited_mst.add(mst)

        cited = cited_by_name.get(norm(name), set())
        targets, drop = delegated_targets(api, mst, cited, parent=name)
        for k, t, j in drop:
            dropped.append({"from": name, "kind": k, "title": t, "조항호목": j})
        if targets or drop:
            cs = ",".join(sorted(cited)) if cited else "없음"
            print(f"  [{name}] 인용조 {cs} → 따라감 {len(targets)}건 / 범위밖 {len(drop)}건")
        time.sleep(SLEEP_SEC)

        for kind, title, 조항 in targets:
            if norm(title) in have_names:
                continue
            is_adm = kind == "위임행정규칙"
            hit = (resolve_admrul(api, title) if is_adm
                   else resolve_law(api, title))
            time.sleep(SLEEP_SEC)
            if not hit:
                print(f"      · {kind} {title} → 현행 조회 실패, 건너뜀")
                continue

            if is_adm:
                nm, eff = hit.get("행정규칙명"), hit.get("시행일자")
                lid, ser = hit.get("행정규칙ID"), hit.get("행정규칙일련번호")
                body = api.body_xml("admrul", ID=ser)
                ltype = hit.get("행정규칙종류")
                pno = hit.get("발령번호")
            else:
                nm, eff = hit.get("법령명한글"), hit.get("시행일자")
                lid, ser = hit.get("법령ID"), hit.get("법령일련번호")
                body = api.body_xml("law", MST=ser)
                ltype = hit.get("법령구분명")
                pno = hit.get("공포번호")

            if norm(nm) in have_names or lid in have_ids:
                continue
            have_names.add(norm(nm))
            have_ids.add(lid)

            fname = f"L1_{safe_name(nm)}_{eff}.xml"
            save_xml(OUT_DIR / fname, body)
            rec = {
                "ref_id": f"D-{lid}", "keyword": title, "doc_type": ltype,
                "target": "admrul" if is_adm else "law", "status": "수집",
                "name": nm, "law_id": lid, "mst": ser, "effective_date": eff,
                "promulgation_no": pno, "law_type": ltype,
                "ministry": hit.get("소관부처명"),
                "cited": [], "cited_raw": "", "parse_notes": [],
                "flags": ["위임수집", f"{kind}←{name}"],
                "article_titles": [],
                "files": [{"file": f"법령 PDF/L1_법령/{fname}", "kind": "현행",
                           "mst": ser, "effective_date": eff}],
                "delegated_from": name, "delegate_kind": kind,
                "delegate_via": 조항, "depth": depth + 1,
            }
            if norm(nm) != norm(title):
                rec["flags"].append(f"제명변경({title}→{nm})")
            extra.append(rec)
            via = f" ({name} {조항})" if 조항 else f" ({name})"
            print(f"      + [{depth+1}단계] {kind} {nm} 시행 {eff}{via}")
            time.sleep(SLEEP_SEC)

            if not is_adm:
                # 시행령·시행규칙은 자기 인용 조를 물려받아 다음 단계로
                cited_by_name.setdefault(norm(nm), cited_by_name.get(norm(title), set()))
                queue.append((nm, ser, depth + 1))

    return extra, dropped


# ── 리포트 ───────────────────────────────────────────────────────
def report(results: list[dict], dropped: list[dict] | None = None) -> None:
    print("\n" + "=" * 74)
    print("수집 리포트")
    print("=" * 74)

    ok = [r for r in results if r.get("status") == "수집"]
    zero = [r for r in results if r.get("status") == "0건"]
    manual = [r for r in results if r.get("status") == "수동수집"]
    err = [r for r in results if r.get("status") == "오류"]
    files = sum(len(r["files"]) for r in results)
    hist = sum(1 for r in results for f in r["files"] if f["kind"] == "시점본")

    print(f"  수집 {len(ok)} / 0건 {len(zero)} / 수동수집 {len(manual)} / 오류 {len(err)}")
    print(f"  XML {files}개 (현행 {files - hist}, 시점본 {hist})")

    print("\n── 수집 목록 ──")
    for r in ok:
        fl = f"  [{','.join(r['flags'])}]" if r["flags"] else ""
        n_hist = sum(1 for f in r["files"] if f["kind"] == "시점본")
        h = f"  +연혁{n_hist}" if n_hist else ""
        print(f"  {r['ref_id']} {r['name']}  시행 {r['effective_date']}  "
              f"{r.get('law_type','')} {r.get('promulgation_no','')}{h}{fl}")

    print("\n── 인용 조문 검수 (조문제목이 맞는지 눈으로 확인) ──")
    for r in ok:
        if not r["article_titles"]:
            continue
        print(f"  {r['ref_id']} {r['name']}   (md 표기: {r['cited_raw'][:50]})")
        for t in r["article_titles"]:
            mark = "   " if t["ok"] else " ⚠ "
            print(f"    {mark}{t['ref']} → {t['title'] if t['ok'] else '(본문에 없음)'}")

    deleg = [r for r in results if "위임수집" in (r.get("flags") or [])]
    if deleg:
        print(f"\n── 위임 추적으로 추가 확보 ({len(deleg)}건) ──")
        for r in sorted(deleg, key=lambda x: (x.get("depth", 9), x["name"])):
            print(f"  [{r.get('depth')}단계] {r['delegate_kind']:<8} {r['name']}  "
                  f"시행 {r['effective_date']}")
            print(f"           ← {r['delegated_from']} {r.get('delegate_via','')}")

    if dropped:
        print(f"\n── 범위 밖으로 제외한 위임 행정규칙 ({len(dropped)}건) ──")
        print("   문서가 인용하지 않은 조에서 위임된 것들. 필요하면 md 참조 조항에 추가하세요.")
        from collections import Counter
        for src, n in Counter(d["from"] for d in dropped).most_common():
            print(f"   {src}: {n}건")

    if zero:
        print("\n── ⚠ 0건 (별칭 매핑 누락 의심) ──")
        for r in zero:
            print(f"  {r['ref_id']} {r['keyword']}")

    if manual:
        print("\n── 수동수집 필요 ──")
        for r in manual:
            print(f"  {r['ref_id']} {r['keyword']}\n       {r.get('reason','')}")

    if err:
        print("\n── ✗ 오류 ──")
        for r in err:
            print(f"  {r['ref_id']} {r['keyword']}: {r.get('reason','')}")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n리포트 저장: {REPORT_PATH}")


def healthcheck(api: Api) -> bool:
    """0건 ≠ 없음. OC 오타/미승인이면 HTTP 200에 빈 결과가 온다."""
    print("헬스체크: query=민법 ...", end=" ")
    hit = resolve_law(api, "민법")
    if not hit:
        print("실패 — OC 키 또는 네트워크를 확인하세요.")
        return False
    print(f"OK (법령ID {hit['법령ID']}, MST {hit['법령일련번호']}, 시행 {hit['시행일자']})")
    return True


def main() -> None:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--healthcheck", action="store_true", help="OC 키 동작만 확인")
    ap.add_argument("--list", action="store_true", help="md 파싱 결과만 출력 (호출 없음)")
    ap.add_argument("--no-history", action="store_true", help="시점본 생략, 현행본만")
    ap.add_argument("--no-delegated", action="store_true",
                    help="위임 추적 생략 (기본은 법률→시행령·시행규칙→위임행정규칙 3단계)")
    ap.add_argument("--delegated-only", action="store_true",
                    help="기존 리포트를 읽어 위임 추적만 수행")
    ap.add_argument("--only", default="", help="쉼표 구분 ref_id (예: L09,R01)")
    ap.add_argument("--report", action="store_true", help="기존 리포트 재출력")
    args = ap.parse_args()

    if args.report:
        if not REPORT_PATH.exists():
            print("리포트가 없습니다. 먼저 수집을 실행하세요.")
            return
        drops = (json.loads(DROP_PATH.read_text(encoding="utf-8"))
                 if DROP_PATH.exists() else [])
        report(json.loads(REPORT_PATH.read_text(encoding="utf-8")), drops)
        return

    master = load_master()
    print(f"마스터 목록: {MD_PATH.name} → {len(master)}건 "
          f"(법령 {sum(1 for m in master if m['target']=='law')}, "
          f"행정규칙 {sum(1 for m in master if m['target']=='admrul')}, "
          f"수동 {sum(1 for m in master if m['target'] is None)})")

    if args.list:
        for m in master:
            tgt = m["target"] or "수동"
            print(f"  {m['ref_id']} [{tgt:>6}] {m['keyword']}")
            if m["cited"]:
                print(f"           인용 {len(m['cited'])}개: {', '.join(m['cited'][:12])}"
                      f"{' ...' if len(m['cited']) > 12 else ''}")
            for n in m["parse_notes"]:
                print(f"           ※ {n}")
        return

    api = Api()
    if not healthcheck(api):
        sys.exit(1)
    if args.healthcheck:
        return

    only = {x.strip() for x in args.only.split(",") if x.strip()} or None
    t0 = time.time()

    if args.delegated_only:
        if not REPORT_PATH.exists():
            print("기존 리포트가 없습니다. 먼저 전체 수집을 실행하세요.")
            sys.exit(1)
        results = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
        results = [r for r in results if "위임수집" not in (r.get("flags") or [])]
    else:
        results = collect(api, master, only, with_history=not args.no_history)

    dropped: list[dict] = []
    if not args.no_delegated:
        print("\n── 위임 추적 (법률 → 시행령·시행규칙 → 위임 행정규칙) ──")
        print("   위임 행정규칙은 '문서가 인용한 조'가 위임한 것만 따라간다.\n")
        base = [r for r in results if r.get("status") == "수집"]
        extra, dropped = crawl_delegated(api, base)
        print(f"\n   위임 계통에서 {len(extra)}건 추가 · 범위 밖 {len(dropped)}건 제외")
        results = results + extra
        DROP_PATH.write_text(json.dumps(dropped, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    report(results, dropped)
    print(f"API 호출 {api.calls}회 / 소요 {time.time() - t0:.1f}초")


if __name__ == "__main__":
    main()
