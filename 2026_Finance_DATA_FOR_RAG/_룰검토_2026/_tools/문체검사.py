# -*- coding: utf-8 -*-
"""문체 규칙 자동 검사 — `_룰검토_2026/_문체_규칙.md` 를 기계로 잰다.

    python 문체검사.py a.md b.md ...

문체 9항목과 인용 손상 3축을 잰다. 인용 3축은 DB 가 있어야 돌고, 없으면 그 셋만 건너뛴다.

읽는 규칙 셋:

1. **코드 펜스 안은 세지 않는다.** 규정 원문·실행 로그·SQL 이 들어 있어 손대면 안 되는 자리다.
   펜스 안을 위반으로 세면 고치려는 사람이 결국 블록을 건드린다(2026-09-04 실제 사고).
2. **인용 3축은 경고지 위반이 아니다.** 요약은 정당하다. 사람이 한 번 보라는 표시다.
   위반으로 세면 정당한 요약까지 걸려서 아무도 안 본다.
3. **걸린 항목이 전부 고칠 것은 아니다.** 출력 끝의 예외를 먼저 보라.

2026-09-04 에 인용 손상 14건이 나왔고 병은 하나였다. 표를 접거나 파일을 다시 쓸 때
칸 안의 원문이 줄어든다. 3축이 각각 다른 자리를 잡는다. 하나로는 안 된다.
"""
import sys
import io
import re
import subprocess
import unicodedata

sys.stdout.reconfigure(encoding='utf-8')

NL = chr(10)
TICK = re.compile('`([^`' + NL + ']{8,})`')
KW = re.compile('「([^」' + NL + ']{8,})」')
BRACKET = re.compile('[«»「」]')

AI_PAT = ['라고 할 수 있', '인 셈이', '다시 말해', '결론적으로', '정리하자면',
          '것이 중요하', '할 필요가 있', '사실상', '본질적으로', '근본적으로',
          '에 대해', '을 통해', '를 통해', '에 의해', '본 문서는',
          '다음과 같', '매우 ', '굉장히', '살펴보', '알아보']

DSN = "postgresql://postgres:devpw@localhost:5432/suddoe"
_conn = None


def db():
    """DB 가 없으면 None. 인용 3축만 건너뛰고 문체 검사는 그대로 돈다."""
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


def norm(s):
    s = unicodedata.normalize('NFC', s)
    for a, b in [('“', '"'), ('”', '"'), ('‘', "'"), ('’', "'")]:
        s = s.replace(a, b)
    s = re.sub(r'-\s*\d+\s*-', '', s)      # 코퍼스 잔재: 본문 한가운데 박힌 쪽번호
    return re.sub(r'\s+', '', s)


def cells_of(line):
    r"""표 한 줄의 칸 목록. `\|` 는 칸 안의 문자라 열 구분자로 세지 않는다.

    타입 표기가 많은 문서(`str \| null`)에서 이걸 안 하면 열 수가 크게 틀린다.
    틀린 값을 보고 표를 접으면 멀쩡한 표를 부순다(2026-09-04 실측).
    """
    s = line.strip()
    if not (s.startswith('|') and s.endswith('|')):
        return None
    # NUL 은 쓰지 않는다. SQL 로 흘러가면 psycopg 가 거부한다(2026-09-04 실측).
    mark = chr(0xE000)   # 사용자 정의 영역. 본문에 나올 일이 없다
    cells = s.replace(chr(92) + '|', mark).strip('|').split('|')
    return [c.replace(mark, chr(92) + '|') for c in cells]


def split_prose(lines):
    """펜스 안을 빈 줄로 바꾼 사본과, 여는 펜스 목록."""
    infence, prose, fences = False, [], []
    for i, l in enumerate(lines):
        if l.lstrip().startswith('```'):
            if not infence:
                fences.append((i + 1, l.strip().lstrip('`').strip()))
            infence = not infence
            prose.append('')
            continue
        prose.append('' if infence else l)
    return prose, fences


def quote_lines(text):
    """인용 블록의 본문 줄. 볼드는 벗긴다 — 안 벗기면 가짜 짧아짐이 쏟아진다."""
    out = []
    for l in text.split(NL):
        if not l.startswith('>'):
            continue
        b = l[1:].strip()
        if not b or b.startswith('—'):
            continue
        out.append(norm(b.replace('**', '')))
    return [x for x in out if x]


def docs_cited(text):
    """이 문서가 실제로 인용하는 doc_id.

    범위를 안 좁히면 제37조 를 법인세법에서 찾아온다(실측 오탐 22건).
    """
    c = db()
    if c is None:
        return []
    found = []
    with c.cursor() as cur:
        for x in set(TICK.findall(text)) | set(KW.findall(text)):
            x = x.strip().rstrip('.')
            cur.execute("select doc_id from corpus.documents where doc_id=%s", (x,))
            if cur.fetchone():
                found.append(x)
                continue
            if '…' in x:                      # L1_…제14차개정 처럼 줄여 쓴 것
                a, b = x.split('…', 1)
                cur.execute("select doc_id from corpus.documents "
                            "where doc_id like %s and doc_id like %s limit 1",
                            (a + '%', '%' + b))
                r = cur.fetchone()
                if r:
                    found.append(r[0])
    return found


def grams(s, n=4):
    return {s[i:i + n] for i in range(max(0, len(s) - n + 1))}


def axis_paraphrase(prose, text):
    """축 1 — 본문에서 조문을 풀어 쓴 문장이 원문보다 짧은가.

    인용 블록은 표시가 있어 독자가 인용인 줄 안다. 풀어 쓴 문장은 모른다.
    그래서 뜻이 뒤집히는 절을 빼도 안 보인다.
    """
    c = db()
    if c is None:
        print("  [축1] 조문 요약 — 건너뜀 (DB 없음)")
        return
    docs = docs_cited(text)
    if not docs:
        print("  [축1] 조문 요약 — 대상 없음 (인용하는 doc_id 없음)")
        return
    hits = 0
    with c.cursor() as cur:
        for i, l in enumerate(prose):
            if l.startswith('>') or l.strip().startswith(('|', '#')):
                continue
            ours = norm(re.sub(r'[`*]', '', l))
            if len(ours) < 20:
                continue
            for jo in sorted(set(re.findall(r'제\d+조', l))):
                cur.execute("select 본문 from corpus.doc_articles "
                            "where doc_id = any(%s) and 조번호=%s "
                            "order by length(본문) desc limit 1", (docs, jo))
                r = cur.fetchone()
                if not r:
                    continue
                orig = norm(r[0])
                if not orig:
                    continue
                ov = len(grams(ours) & grams(orig)) / max(len(grams(ours)), 1)
                if ov < 0.30 or ours in orig or len(ours) >= len(orig) * 0.70:
                    continue
                hits += 1
                if hits <= 3:
                    print("  [축1 경고] 조문 요약이 원문보다 짧다. 뜻이 뒤집히는 절을 뺐는지 봐라.")
                    print(f"      {i+1}줄 / {jo} / 겹침 {ov:.0%}")
                    print(f"      우리: {l.strip()[:70]}")
                    print(f"      원문: {r[0].strip()[:70]}")
    print(f"  [축1] 조문 요약 경고 {hits}건")


def axis_shrunk(path, text):
    """축 2 — 고치기 전(HEAD)보다 인용이 짧아졌는가."""
    try:
        rel = path.replace('\\', '/')
        old = subprocess.run(['git', 'show', 'HEAD:' + rel],
                             capture_output=True).stdout.decode('utf-8')
    except Exception as e:
        print(f"  [축2] 인용 축소 — 건너뜀 ({type(e).__name__})")
        return
    if not old.strip():
        print("  [축2] 인용 축소 — 건너뜀 (HEAD 에 없는 파일)")
        return
    now = quote_lines(text)
    hits = 0
    for o in quote_lines(old):
        if any(o in n for n in now):
            continue
        shorter = [n for n in now if n and n in o]
        if not shorter:
            continue
        n = max(shorter, key=len)
        hits += 1
        if hits <= 3:
            print("  [축2 경고] 인용이 고치기 전보다 짧아졌다. 빠진 절이 뜻을 바꾸는지 봐라.")
            print(f"      전: {o[:70]}")
            print(f"      후: {n[:70]}")
    print(f"  [축2] 인용 축소 경고 {hits}건")


def axis_cell(prose):
    """축 3 — 표 칸의 앞 15자는 코퍼스에 있는데 칸 전체는 없으면 잘린 인용이다.

    표 칸은 인용 블록이 아니라 축1·축2 가 놓친다. 손상 14건 중 9건이 이 자리였다.
    """
    c = db()
    if c is None:
        print("  [축3] 표 칸 지문 — 건너뜀 (DB 없음)")
        return
    q = ("select 1 from corpus.doc_articles "
         "where replace(replace(본문, ' ', ''), chr(10), '') like %s limit 1")
    hits = 0
    with c.cursor() as cur:
        for i, l in enumerate(prose):
            for cell in (cells_of(l) or []):
                v = norm(cell.replace('**', ''))
                if len(v) < 25:
                    continue
                cur.execute(q, ('%' + v[:15] + '%',))
                if not cur.fetchone():
                    continue
                cur.execute(q, ('%' + v + '%',))
                if cur.fetchone():
                    continue
                hits += 1
                if hits <= 3:
                    print("  [축3 경고] 표 칸이 원문을 잘라 담은 것으로 보인다.")
                    print(f"      {i+1}줄: {cell.strip()[:70]}")
    print(f"  [축3] 표 칸 지문 경고 {hits}건")


def check(path):
    try:
        t = io.open(path, encoding='utf-8').read()
    except Exception as e:
        print(f"=== {path}")
        print(f"  읽기 실패 — {type(e).__name__}: {e}")
        return
    L = t.split(NL)
    prose, fences = split_prose(L)
    body = NL.join(prose)
    print(f"=== {path.split('/')[-1]}  ({len(L)}줄)")

    r = body.count('\U0001F534')
    print(f"  🔴 {r}개" + ("  ← 3개까지" if r > 3 else ""))

    heads = [(i + 1, len(m.group(1))) for i, l in enumerate(L)
             if (m := re.match(r'^(#{1,6}) ', l))]
    h2 = [h for h in heads if h[1] == 2]
    print(f"  ## {len(h2)}개"
          + ("  ← 3~7 (찾아보는 문서는 예외)" if not 3 <= len(h2) <= 7 else ""))

    skips = [(a[0], a[1], b[1]) for a, b in zip(heads, heads[1:]) if b[1] > a[1] + 1]
    print(f"  제목 수준 건너뜀 {len(skips)}건" + (f"  ← {skips[:3]}" if skips else ""))

    br = [i + 1 for i, l in enumerate(prose)
          if not l.startswith('>') and BRACKET.search(l)]
    print(f"  특수괄호 {len(br)}줄" + (f"  ← {br[:6]}" if br else ""))

    over = {}
    for i, l in enumerate(prose):
        cs = cells_of(l)
        if cs and len(cs) >= 5:
            over.setdefault(len(cs), []).append(i + 1)
    if over:
        for n, ls in sorted(over.items()):
            print(f"  표 {n}열 {len(ls)}줄  ← {ls[:3]}  (색인표는 예외)")
    else:
        print("  표 열수 OK")

    nolang = [f[0] for f in fences if not f[1]]
    print(f"  펜스 언어 없음 {len(nolang)}개" + (f"  ← {nolang[:5]}" if nolang else ""))

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

    b = sum(len(m) for m in re.findall(r'\*\*(.+?)\*\*', body))
    ratio = b / max(len(body), 1) * 100
    print(f"  볼드 비율 {ratio:.1f}%" + ("  ← 20% 초과" if ratio > 20 else ""))

    axis_paraphrase(prose, t)
    axis_shrunk(path, t)
    axis_cell(prose)


for p in sys.argv[1:]:
    check(p)

print()
print("인용 3축은 경고다. 위반이 아니다. 요약은 정당하고, 사람이 한 번 보라는 표시다.")
print("예외 — 걸려도 고치지 않는 것:")
print("  · 찾아보는 문서(대조표·색인표)는 ## 이 많아도 둔다")
print("  · 색인표는 열이 많은 게 기능이다. 접으면 색인이 죽는다")
print("  · 파일명·경로·명령어 안·규정 원문 인용·법령명·용어 치환 예시의 괄호는 문체가 아니다")
