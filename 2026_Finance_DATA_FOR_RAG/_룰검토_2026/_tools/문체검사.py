# -*- coding: utf-8 -*-
"""문체 규칙 자동 검사 — `_룰검토_2026/_문체_규칙.md` 9개 절을 기계로 잰다.

    python 문체검사.py a.md b.md ...

재는 것: 🔴 개수 · `##` 개수 · 제목 수준 건너뛰기 · 특수 괄호 · 표 열수 ·
6줄+ 문단 · 코드 펜스 언어 지정 · AI 워딩(1-5절) · 볼드 비율.

🔴 **코드 펜스 안은 세지 않는다.** 규정 원문·실행 로그·SQL 이 들어 있어 손대면 안 되는 자리다.
펜스 안을 위반으로 세면 고치려는 사람이 결국 블록을 건드린다(2026-09-04 실제 사고).
그래서 펜스 안을 빈 줄로 바꾼 `prose` 를 만들어 **모든 본문 검사가 그것만 본다.**
`L` 은 줄 수 세기와 제목 검사에만 쓴다.

걸린 항목이 전부 고칠 것은 아니다. 규칙 문서의 예외를 먼저 보라.
"""
import sys, io, re, unicodedata

sys.stdout.reconfigure(encoding='utf-8')

NO_NL_TICK = re.compile(r'`([^`
]{8,})`')
NO_NL_KW = re.compile(r'「([^」
]{8,})」')

AI_PAT = ['라고 할 수 있', '인 셈이', '다시 말해', '결론적으로', '정리하자면',
          '것이 중요하', '할 필요가 있', '사실상', '본질적으로', '근본적으로',
          '에 대해', '을 통해', '를 통해', '에 의해', '본 문서는',
          '다음과 같', '매우 ', '굉장히', '살펴보', '알아보']


def split_prose(L):
    """코드 펜스 안을 빈 줄로 바꾼 사본과, 여는 펜스 목록을 함께 돌려준다."""
    infence, prose, fences = False, [], []
    for i, l in enumerate(L):
        if l.lstrip().startswith('```'):
            if not infence:
                fences.append((i + 1, l.strip().lstrip('`').strip()))
            infence = not infence
            prose.append('')
            continue
        prose.append('' if infence else l)
    return prose, fences


DSN = "postgresql://postgres:devpw@localhost:5432/suddoe"
_conn = None


def db():
    """DB 가 없으면 None. 조문 요약 검사만 건너뛰고 나머지는 그대로 돈다."""
    global _conn
    if _conn is False:
        return None
    if _conn is None:
        try:
            import psycopg
            _conn = psycopg.connect(DSN, connect_timeout=3)
            _conn.read_only = True
        except Exception:
            _conn = False
            return None
    return _conn


def _n(s):
    s = unicodedata.normalize('NFC', s)
    for a, b in [('“', '"'), ('”', '"'), ('‘', "'"), ('’', "'")]:
        s = s.replace(a, b)
    s = re.sub(r'-\s*\d+\s*-', '', s)
    return re.sub(r'\s+', '', s)


def _docs_in(text):
    """이 문서가 실제로 인용하는 doc_id 만 고른다.

    범위를 안 좁히면 제37조 를 법인세법에서 찾아온다(실측 오탐 22건).
    엉뚱한 경고가 쏟아지면 아무도 검사기를 안 본다.
    """
    c = db()
    if c is None:
        return []
    cands = (set(re.findall(NO_NL_TICK, text))
             | set(re.findall(NO_NL_KW, text)))
]{8,})」', text))
    found = []
    with c.cursor() as cur:
        for x in cands:
            x = x.strip().rstrip('.')
            cur.execute("select doc_id from corpus.documents where doc_id=%s", (x,))
            if cur.fetchone():
                found.append(x)
                continue
            if '…' in x:            # L1_…제14차개정_20251223 처럼 줄여 쓴 것
                a, b = x.split('…', 1)
                cur.execute("select doc_id from corpus.documents "
                            "where doc_id like %s and doc_id like %s limit 1",
                            (a + '%', '%' + b))
                r = cur.fetchone()
                if r:
                    found.append(r[0])
    return found


def _grams(s, n=4):
    return {s[i:i + n] for i in range(max(0, len(s) - n + 1))}


def paraphrase(prose, text):
    """조문을 본문에서 풀어 쓴 문장이 원문보다 짧으면 경고한다.

    인용 블록은 표시가 있어 독자가 인용인 줄 안다. 풀어 쓴 문장은 모른다.
    그래서 뜻이 뒤집히는 절을 빼도 안 보인다(2026-09-04 두 세션이 같은 절을 뺐다).

    경고지 위반이 아니다. 요약은 정당하다. 사람이 한 번 보라는 표시다.
    그래서 세 가지로 좁힌다. 이 문서가 인용하는 doc 안에서만 찾고,
    글자가 30% 넘게 겹칠 때만 그 조를 말한 것으로 보고, 원문의 70% 미만일 때만 경고한다.
    """
    c = db()
    if c is None:
        print("  조문 요약 검사 — 건너뜀 (DB 접속 안 됨)")
        return
    docs = _docs_in(text)
    if not docs:
        print("  조문 요약 검사 — 대상 없음 (인용하는 doc_id 를 못 찾음)")
        return
    hits = 0
    with c.cursor() as cur:
        for i, l in enumerate(prose):
            if l.startswith('>') or l.strip().startswith(('|', '#')):
                continue
            ours = _n(re.sub(r'[`*]', '', l))
            if len(ours) < 20:
                continue
            for jo in sorted(set(re.findall(r'제\d+조', l))):
                cur.execute("select 본문 from corpus.doc_articles "
                            "where doc_id = any(%s) and 조번호=%s "
                            "order by length(본문) desc limit 1", (docs, jo))
                r = cur.fetchone()
                if not r:
                    continue
                orig = _n(r[0])
                og = _grams(orig)
                if not og:
                    continue
                overlap = len(_grams(ours) & og) / max(len(_grams(ours)), 1)
                if overlap < 0.30:
                    continue          # 그 조를 풀어 쓴 문장이 아니다
                if ours in orig or len(ours) >= len(orig) * 0.70:
                    continue
                hits += 1
                if hits <= 3:
                    print("  [경고] 조문 요약이 원문보다 짧다. 뜻이 뒤집히는 절을 뺐는지 확인해라.")
                    print(f"    {i+1}줄 / 조: {jo} / 겹침 {overlap:.0%}")
                    print(f"    우리: {l.strip()[:70]}")
                    print(f"    원문: {r[0].strip()[:70]}")
    print(f"  조문 요약 경고 {hits}건" + ("  (경고지 위반이 아니다)" if hits else ""))


def quote_lines(text):
    """인용 블록의 본문 줄만. 볼드 표시는 벗긴다(안 벗기면 가짜 짧아짐이 쏟아진다)."""
    out = []
    for l in text.split('
'):
        if not l.startswith('>'):
            continue
        b = l[1:].strip()
        if b.startswith('—') or not b:
            continue
        out.append(_n(b.replace('**', '')))
    return out


def shrunk_vs_head(path, text):
    """축 1 — 고치기 전(HEAD)보다 인용이 짧아졌는지."""
    import subprocess
    try:
        rel = path.replace('\', '/')
        old = subprocess.run(['git', 'show', f'HEAD:{rel}'],
                             capture_output=True).stdout.decode('utf-8')
        if not old.strip():
            print("  인용 축소 검사 — 건너뜀 (HEAD 에 없는 파일)")
            return
    except Exception as e:
        print(f"  인용 축소 검사 — 건너뜀 ({type(e).__name__})")
        return
    now = quote_lines(text)
    hits = 0
    for o in quote_lines(old):
        if o in now or any(o in n for n in now):
            continue
        cand = [n for n in now if n and (n in o)]
        if not cand:
            continue
        n = max(cand, key=len)
        hits += 1
        if hits <= 3:
            print("  [경고] 인용이 고치기 전보다 짧아졌다. 빠진 절이 뜻을 바꾸는지 봐라.")
            print(f"    전: {o[:70]}")
            print(f"    후: {n[:70]}")
    print(f"  인용 축소 경고 {hits}건" + ("  (경고지 위반이 아니다)" if hits else ""))


def cell_fingerprint(prose):
    """축 2 — 표 칸의 앞 15자는 코퍼스에 있는데 칸 전체는 없으면 잘린 인용이다."""
    c = db()
    if c is None:
        print("  표 칸 지문 검사 — 건너뜀 (DB 접속 안 됨)")
        return
    hits = 0
    with c.cursor() as cur:
        for i, l in enumerate(prose):
            s0 = l.strip()
            if not (s0.startswith('|') and s0.endswith('|')):
                continue
            for cell in s0.replace('\|', ' ').strip('|').split('|'):
                v = _n(cell.replace('**', ''))
                if len(v) < 25:
                    continue
                head = v[:15]
                cur.execute("select 1 from corpus.doc_articles "
                            "where replace(replace(본문, ' ', ''), chr(10), '') like %s limit 1",
                            ('%' + head + '%',))
                if not cur.fetchone():
                    continue
                cur.execute("select 1 from corpus.doc_articles "
                            "where replace(replace(본문, ' ', ''), chr(10), '') like %s limit 1",
                            ('%' + v + '%',))
                if cur.fetchone():
                    continue
                hits += 1
                if hits <= 3:
                    print("  [경고] 표 칸이 원문을 잘라 담은 것으로 보인다.")
                    print(f"    {i+1}줄: {cell.strip()[:70]}")
    print(f"  표 칸 지문 경고 {hits}건" + ("  (경고지 위반이 아니다)" if hits else ""))


def check(path):
    try:
        t = io.open(path, encoding='utf-8').read()
    except Exception as e:
        print(f"=== {path}")
        print(f"  읽기 실패 — {type(e).__name__}: {e}")
        return
    L = t.split('\n')
    prose, fences = split_prose(L)
    body = '\n'.join(prose)
    print(f"=== {path.split('/')[-1]}  ({len(L)}줄)")

    r = body.count('\U0001F534')
    print(f"  🔴 {r}개" + ("  ← 3개까지" if r > 3 else ""))

    heads = [(i + 1, len(m.group(1)), l) for i, l in enumerate(L)
             if (m := re.match(r'^(#{1,6}) ', l))]
    h2 = [h for h in heads if h[1] == 2]
    print(f"  ## {len(h2)}개" + ("  ← 3~7 (찾아보는 문서는 예외)" if not 3 <= len(h2) <= 7 else ""))

    skips = [(a[0], a[1], b[1]) for a, b in zip(heads, heads[1:]) if b[1] > a[1] + 1]
    print(f"  제목 수준 건너뜀 {len(skips)}건" + (f"  ← {skips[:3]}" if skips else ""))

    br = [i + 1 for i, l in enumerate(prose)
          if not l.startswith('>') and re.search(r'[«»「」]', l)]
    print(f"  특수괄호 {len(br)}줄" + (f"  ← {br[:6]}" if br else ""))

    over = {}
    for i, l in enumerate(prose):
        s = l.strip()
        if s.startswith('|') and s.endswith('|'):
            # `\|` 는 칸 안의 문자다. 열 구분자가 아니다(str \| null 같은 타입 표기).
            n = len(s.replace('\|', ' ').strip('|').split('|'))
            if n >= 5:
                over.setdefault(n, []).append(i + 1)
    if over:
        for n, ls in sorted(over.items()):
            print(f"  표 {n}열 {len(ls)}줄  ← {ls[:3]}  (색인표는 예외)")
    else:
        print("  표 열수 OK")

    nolang = [f for f in fences if not f[1]]
    print(f"  펜스 언어 없음 {len(nolang)}개" + (f"  ← {[f[0] for f in nolang][:5]}" if nolang else ""))

    run, start, longs = [], 0, []
    for i, l in enumerate(prose):
        s = l.strip()
        if s and not s.startswith(('|', '#', '>', '-', '`')) and not re.match(r'^\d+\.', s):
            if not run:
                start = i + 1
            run.append(s)
        else:
            if len(run) >= 6:
                longs.append((start, len(run)))
            run = []
    if len(run) >= 6:
        longs.append((start, len(run)))
    print(f"  6줄+ 문단 {len(longs)}개" + (f"  ← {longs[:5]}" if longs else ""))

    ai = []
    for i, l in enumerate(prose):
        if l.startswith('>'):
            continue
        if l.lstrip().startswith('즉,'):
            ai.append((i + 1, '즉,'))
        for pt in AI_PAT:
            if pt in l:
                ai.append((i + 1, pt))
    print(f"  AI 워딩 {len(ai)}건" + (f"  ← {ai[:5]}" if ai else ""))

    paraphrase(prose, t)
    shrunk_vs_head(path, t)
    cell_fingerprint(prose)

    b = sum(len(m) for m in re.findall(r'\*\*(.+?)\*\*', body))
    ratio = b / max(len(body), 1) * 100
    print(f"  볼드 비율 {ratio:.1f}%" + ("  ← 20% 초과" if ratio > 20 else ""))


for p in sys.argv[1:]:
    check(p)

print()
print("예외 — 걸려도 고치지 않는 것:")
print("  · 찾아보는 문서(대조표·색인표)는 ## 이 많아도 둔다")
print("  · 색인표는 열이 많은 게 기능이다. 접으면 색인이 죽는다")
print("  · 파일명·경로·명령어 안·규정 원문 인용·법령명·용어 치환 예시의 괄호는 문체가 아니다")
