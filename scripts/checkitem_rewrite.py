"""corpus.check_items(구분='결제전')의 항목·설명을 LLM 으로 다시 쓴다 — 일회성 배치.

레인 W (오너 지시 2026-09-07, 중앙 ai-33 확정 방식):
  실시간 생성이 아니라 «한 번 LLM 으로 뽑아서 DB 에 고정». 이유는
  Handoff_인수인계_QA최종_0907.md #레인W 및 ai-33 지시 참조 — 체크항목은
  닫힌 집합(43건)이고, 절차 안내를 실시간 생성하면 환각 위험이 크다.

흐름
  1. corpus.check_items(구분='결제전') 43행 + 근거 조항 원문(corpus.doc_articles) 로드
  2. ①생성 — 근거 원문을 프롬프트에 넣고 새 항목(≤20자 명령형)·새 설명(≤45자 일상어) 생성
  3. ②검수 — 새 설명이 원문의 절차·주체·시점을 바꿨는지 별도 LLM 호출로 대조
  4. scratchpad/W_체크항목_문안.md 에 표 + UPDATE 문(실행 안 함) 출력

🔴 DB 쓰기는 안 한다 — 이 스크립트는 읽기 전용 + 파일 출력뿐이다. 실행은 중앙(ai-33) 몫.
"""
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "_lib"))
import db  # noqa: E402

VLLM_URL = os.environ.get("VLLM_URL", "").rstrip("/")
if not VLLM_URL:
    sys.exit("VLLM_URL 이 없다 — 지금 열려 있는 팟 주소를 넣어라")

생성_스키마 = {
    "type": "object",
    "properties": {
        "새항목": {"type": "string"},
        "새설명": {"type": "string"},
    },
    "required": ["새항목", "새설명"],
}

검수_스키마 = {
    "type": "object",
    "properties": {
        "절차_유지": {"type": "boolean"},
        "주체_유지": {"type": "boolean"},
        "시점_유지": {"type": "boolean"},
        "숫자조건_유지": {"type": "boolean"},
        "사유": {"type": "string"},
    },
    "required": ["절차_유지", "주체_유지", "시점_유지", "숫자조건_유지", "사유"],
}


def llm_호출(프롬프트: str, 스키마: dict, *, max_tokens: int = 1500) -> dict:
    # 🔴 500 으로 돌렸다가 39/43 이 JSONDecodeError 로 죽었다(2026-09-07 1차 실행).
    #    Qwen3 는 thinking 이 기본이라 사고에 토큰을 다 쓰면 content 가 빈 채/잘린 채
    #    끝난다(`docs/8_운영/8-3_GPU.md` 가 이미 경고한 실패모드) — max_tokens 부족이지
    #    guided_json 이나 파싱 로직 문제가 아니었다.
    본문 = {
        "model": "Qwen/Qwen3-32B-AWQ",
        "messages": [{"role": "user", "content": 프롬프트}],
        "temperature": 0,
        "max_tokens": max_tokens,
        "guided_json": 스키마,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        f"{VLLM_URL}/v1/chat/completions",
        data=json.dumps(본문, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "suddoe/1.0"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read().decode())
    내용 = d["choices"][0]["message"]["content"]
    if "</think>" in 내용:
        내용 = 내용.rsplit("</think>", 1)[1].strip()
    if 내용.startswith("```"):
        내용 = 내용.split("```")[1]
        if 내용.startswith("json"):
            내용 = 내용[4:]
    return json.loads(내용.strip())


def 생성_프롬프트(항목: dict, 근거원문: list[str]) -> str:
    원문블록 = "\n---\n".join(근거원문)
    return f"""너는 정부 창업지원금 사업비 집행 안내문을 쉬운 말로 다시 쓰는 편집자다.

[근거 조항 원문 — 이 안의 사실만 써라. 지어내지 마라]
{원문블록}

[기존 항목] {항목['항목']}
[기존 설명] {항목['설명']}

작업:
1. 새항목 — 명령형 한 줄, 20자 이내. 무엇을 하라는 건지 바로 알 수 있게.
2. 새설명 — 한 문장, 45자 이내, 일상어. 조번호·L1·L2·S번호 같은 기호는 쓰지 마라.
   근거 원문에 있는 금액·기간·비율 등 숫자 조건은 반드시 그대로 남겨라.
   근거 원문에 없는 절차·주체·시점을 새로 만들지 마라.

guided_json 스키마에 맞는 JSON 하나만 출력해라."""


def 검수_프롬프트(항목: dict, 새항목: str, 새설명: str, 근거원문: list[str]) -> str:
    원문블록 = "\n---\n".join(근거원문)
    return f"""너는 아래 "새설명"이 "근거 조항 원문"의 사실을 왜곡했는지 검수하는 감사자다.

[근거 조항 원문]
{원문블록}

[기존 설명(원본)] {항목['설명']}
[새항목] {새항목}
[새설명] {새설명}

새설명이 원문 대비 다음을 바꿨는지 각각 true/false 로 답해라(바뀌지 않았으면 true, 즉 "유지"):
- 절차_유지: 해야 할 행위·절차(예: 사전심의→사후보고 같은 뒤집힘)가 안 바뀌었는가
- 주체_유지: 누가 하는지(예: 주관기관→창업기업)가 안 바뀌었는가
- 시점_유지: 언제 하는지(예: 사전→사후)가 안 바뀌었는가
- 숫자조건_유지: 금액·기간·비율 등 숫자 조건이 원문과 일치하는가
- 사유: 한 문장으로 판단 근거

guided_json 스키마에 맞는 JSON 하나만 출력해라."""


def main() -> None:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT code, 사업명, 비목, 항목, 설명, 근거, 유형 "
            "FROM corpus.check_items WHERE 구분='결제전' ORDER BY code"
        ).fetchall()
    items = [
        {"code": r[0], "사업명": r[1], "비목": r[2], "항목": r[3],
         "설명": r[4], "근거": r[5], "유형": r[6]}
        for r in rows
    ]

    # 🔴 이미 검수 통과한 code 는 다시 안 부른다 — 토큰 절약(2026-09-07, 중단 재개분).
    #    이전 산출물의 "✅" 행에서 code 를 뽑는다. 파일이 없거나 형식이 다르면 그냥 전부 돈다.
    이미완료 = set()
    보존행: dict[str, str] = {}
    보존UPDATE: list[str] = []
    try:
        with open("scratchpad/W_체크항목_문안.md", encoding="utf-8") as f:
            content = f.read()
        for line in content.splitlines():
            if line.startswith("|") and line.rstrip().endswith("| ✅ |"):
                code = line.split("|")[1].strip()
                이미완료.add(code)
                보존행[code] = line
        for line in content.splitlines():
            if line.strip().startswith("UPDATE corpus.check_items"):
                보존UPDATE.append(line)
    except FileNotFoundError:
        pass
    if 이미완료:
        print(f"이미 통과한 {len(이미완료)}건 건너뜀: {sorted(이미완료)}")
        items = [it for it in items if it["code"] not in 이미완료]
    print(f"이번 실행 대상 {len(items)}행")

    # 근거 원문 캐시
    원문캐시: dict[tuple[str, str], str] = {}
    with db.connect() as conn:
        모든쌍 = {(g["doc_id"], g["조번호"]) for it in items for g in it["근거"]}
        for doc_id, jo in 모든쌍:
            r = conn.execute(
                "SELECT 본문 FROM corpus.doc_articles WHERE doc_id=%s AND 조번호=%s LIMIT 1",
                (doc_id, jo),
            ).fetchone()
            원문캐시[(doc_id, jo)] = r[0] if r else "(원문 못 찾음)"

    결과 = []
    for i, it in enumerate(items, 1):
        근거원문 = [원문캐시[(g["doc_id"], g["조번호"])] for g in it["근거"]]
        근거표기 = "; ".join(f"{g['doc_id']}·{g['조번호']}" for g in it["근거"])
        print(f"[{i}/{len(items)}] {it['code']}")
        try:
            생성 = llm_호출(생성_프롬프트(it, 근거원문), 생성_스키마)
            검수 = llm_호출(
                검수_프롬프트(it, 생성["새항목"], 생성["새설명"], 근거원문), 검수_스키마
            )
            검수OK = all([검수["절차_유지"], 검수["주체_유지"],
                        검수["시점_유지"], 검수["숫자조건_유지"]])
        except Exception as e:  # noqa: BLE001
            print(f"  🔴 실패: {type(e).__name__}: {e}")
            생성 = {"새항목": it["항목"], "새설명": it["설명"]}
            검수 = {"사유": f"LLM 호출 실패({type(e).__name__}) — 원문 유지"}
            검수OK = False
        결과.append({**it, "근거표기": 근거표기, "새항목": 생성["새항목"],
                    "새설명": 생성["새설명"], "검수": 검수, "검수OK": 검수OK})

    # 전체 43행 기준 통과/전체 집계 — 이번 실행분 + 이전에 보존한 ✅ 행 합산
    전체행수 = len(items) + len(이미완료)
    새로통과 = sum(1 for r in 결과 if r["검수OK"])
    통과 = 새로통과 + len(이미완료)

    # ── 출력 (전체 43행을 code 순으로 다시 정렬해 표기 — 보존행 + 이번 실행분) ──
    코드별결과 = {r["code"]: r for r in 결과}
    with db.connect() as conn:
        전체코드순 = [r[0] for r in conn.execute(
            "SELECT code FROM corpus.check_items WHERE 구분='결제전' ORDER BY code").fetchall()]

    out = ["# W — 「결제 전 확인」 문구 재작성 (LLM 생성 + 검수, 2026-09-07)",
           "",
           f"대상 {전체행수}행(구분='결제전') · 모델 Qwen/Qwen3-32B-AWQ ·",
           "검수는 절차/주체/시점/숫자조건 4항목 전부 「유지」일 때만 OK.",
           "",
           "| code | 기존항목 | 새항목 | 기존설명 | 새설명 | 근거조항 | 검수 |",
           "|---|---|---|---|---|---|---|"]
    for code in 전체코드순:
        if code in 보존행:
            out.append(보존행[code])
            continue
        r = 코드별결과[code]
        상태 = "✅" if r["검수OK"] else f"🔴 원문유지 — {r['검수'].get('사유','')}"
        새항목 = r["새항목"] if r["검수OK"] else r["항목"]
        새설명 = r["새설명"] if r["검수OK"] else r["설명"]
        out.append(
            f"| {r['code']} | {r['항목']} | {새항목} | {r['설명']} | {새설명} | "
            f"{r['근거표기']} | {상태} |"
        )

    out += ["", "## UPDATE 문 (검수 OK 행만 · 실행 금지, 중앙 몫)", "```sql"]
    out.extend(보존UPDATE)
    for r in 결과:
        if r["검수OK"]:
            새항목_esc = r["새항목"].replace("'", "''")
            새설명_esc = r["새설명"].replace("'", "''")
            out.append(
                f"UPDATE corpus.check_items SET 항목='{새항목_esc}', "
                f"설명='{새설명_esc}' WHERE code='{r['code']}';"
            )
    out.append("```")

    out += ["", f"## 요약: {통과}/{전체행수}건 검수 통과 · {전체행수-통과}건 원문 유지"]

    with open("scratchpad/W_체크항목_문안.md", "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print(f"완료: {통과}/{len(결과)} 통과 → scratchpad/W_체크항목_문안.md")


if __name__ == "__main__":
    main()
