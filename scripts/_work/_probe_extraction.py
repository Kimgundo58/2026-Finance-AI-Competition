# -*- coding: utf-8 -*-
"""corpus.documents 의 PDF 원본에서 dedupe 여부만 실측한다 (retag_extraction.py 근거).

pdftext.extract_meta() 와 **동일한 판정**을 쓰되 전체 추출은 하지 않는다:
    probe = pages[min(4, n-1)].extract_text();  dedupe = dup_ratio(probe) > 0.35
전체 추출은 다단·4분면 재조립까지 도는 탓에 대형 사례집에서 분 단위로 걸린다.
dedupe 플래그 자체는 이 한 쪽만 보고 정해지므로 결과가 같다.

결과: scripts/_work/_extraction_probe.json
"""
from __future__ import annotations
import io, json, os, sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import psycopg
import pdfplumber
import pdftext

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")

with psycopg.connect(DSN) as c:
    rows = c.execute("""
        SELECT doc_id, layer, index_target, parse_quality, src_path
        FROM corpus.documents
        WHERE src_path NOT LIKE '%.xml'
        ORDER BY src_path
    """).fetchall()

out = []
for doc_id, layer, itgt, pq, src in rows:
    p = ROOT / src.replace("\\", "/")
    rec = {"doc_id": doc_id, "layer": layer, "index_target": itgt,
           "parse_quality": pq, "src_path": src, "exists": p.exists()}
    if p.exists() and p.suffix.lower() == ".pdf":
        try:
            with pdfplumber.open(p) as pdf:
                n = len(pdf.pages)
                probe = pdf.pages[min(4, n - 1)].extract_text() or "" if n else ""
                r = pdftext.dup_ratio(probe)
                rec.update(pages=n, dup_ratio=round(r, 3),
                           dedupe=r > pdftext.DUP_THRESHOLD,
                           probe_chars=len(probe))
        except Exception as e:                     # noqa: BLE001
            rec["error"] = f"{type(e).__name__}: {e}"
    print("dedupe=%-5s dup=%-6s p=%-4s %s"
          % (rec.get("dedupe"), rec.get("dup_ratio"), rec.get("pages"), src),
          flush=True)
    out.append(rec)

(Path(__file__).parent / "_extraction_probe.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print("\n총 %d건 / dedupe %d건" % (len(out), sum(1 for r in out if r.get("dedupe"))))
