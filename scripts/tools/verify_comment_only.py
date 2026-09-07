# -*- coding: utf-8 -*-
"""주석·docstring 만 바뀌었는지 검증한다 — 작업 트리와 HEAD 의 AST(docstring 제거) 를 비교.

    PYTHONIOENCODING=utf-8 python scripts/tools/verify_comment_only.py <파일>...
    PYTHONIOENCODING=utf-8 python scripts/tools/verify_comment_only.py --changed   # git 변경분 전부
"""
import ast, subprocess, sys, pathlib

def _strip_docstrings(tree):
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = n.body
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
               and isinstance(body[0].value.value, str):
                body.pop(0)
                if not body:
                    body.append(ast.Pass())
    return tree

def _dump(src):
    return ast.dump(_strip_docstrings(ast.parse(src)), include_attributes=False)

def _sql_body(src):
    """`--` 줄주석과 `/* */` 블록주석을 걷고 공백을 정규화한다 (문자열 안의 -- 는 구분 못 한다)."""
    import re
    s = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    s = re.sub(r"--[^\n]*", " ", s)
    return " ".join(s.split())

def check(path):
    new = pathlib.Path(path).read_text(encoding="utf-8")
    r = subprocess.run(["git", "show", f"HEAD:{path}"], capture_output=True)
    if r.returncode != 0:
        return "NEW(HEAD 에 없음)"
    old = r.stdout.decode("utf-8")
    if path.endswith(".sql"):
        return "OK" if _sql_body(old) == _sql_body(new) else "CHANGED — SQL 본문이 바뀌었다"
    try:
        return "OK" if _dump(old) == _dump(new) else "CHANGED — 코드가 바뀌었다"
    except SyntaxError as e:
        return f"SYNTAX — {e}"

if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["--changed"]:
        out = subprocess.run(["git", "-c", "core.quotepath=off", "diff", "--name-only", "HEAD", "--", "*.py", "*.sql"],
                             capture_output=True, text=True, encoding="utf-8").stdout
        args = [l for l in out.splitlines() if l.strip()]
    bad = 0
    for p in args:
        p = p.replace("\\", "/")
        res = check(p)
        print(f"{res:8s} {p}")
        bad += res != "OK"
    sys.exit(1 if bad else 0)
