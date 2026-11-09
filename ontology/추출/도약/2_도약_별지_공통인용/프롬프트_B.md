Input folder: ontology/추출/도약/2_도약_별지_공통인용/작업_B
You are extractor B. Work independently: another extractor works on the same provisions; do not look for, open or ask about its folder, its output or any file outside the input folder except the documents the prompt below allows.
In the prompt below, 입력_원문.md, 입력_문서키.json, 입력_기존노드.json and "the input folder" mean the files in ontology/추출/도약/2_도약_별지_공통인용/작업_B.

You extract concepts, relations and norms from Korean startup-grant regulations, one provision at a time.
Read docs/5_룰/5-5_규범_구조.md, docs/5_룰/5-7_원문_추출_절차.md (section 추출표) and docs/5_룰/5-8b_원천_JSON_형식.md first.
Input: 입력_원문.md (provisions with IDs), 입력_문서키.json, 입력_기존노드.json.
For every provision ID in the input, write one 추출표 block in the output format below. Do not skip provisions; write
"해당 없음" when a provision yields nothing.
Rules: quote excerpts verbatim from 입력_원문.md (after the normalization in 5-8b); one norm per effect per sentence;
operators as written (이상/초과/이하/미만); unknown things go to 모르는 것, never guessed; reuse IDs from 입력_기존노드.json
when the concept already exists; never open corpus.rules, corpus.item_alias, golden sets or any file outside the input folder.
Open only the three input files, the three documents above and the section 출력 형식 of docs/5_룰/5-7b_추출_에이전트_지침.md.
If the input folder holds any file other than the three inputs, stop and report it.
Every 금지 and 의무 gets `문장`: one plain Korean sentence in 합니다체 that the user will read, faithful to the provision, with no IDs,
no table separators, no double quotes and no " | ". Exception: a 금지 whose excerpt is only a table title leaves 문장 empty.
Use the narrowest node the text names as 대상. When a premise in the provision limits a norm, carry it into that norm's `조건` with its
excerpt; never let a 금지 block more than the text limits.
Write 임계값 only when the number compares with the amount of the expense or contract being judged, or with a running total that fits
누적범위. A number about anything else (program size, total received) goes into the `조건` of the 의무 it triggers. A 한도 that fails
the same test stays a 한도, with the reason in 모르는 것.
Add `기한일 YYYY-MM-DD` only for one end date of a submission or application duty, after which the guidance no longer matters. Leave it
out for a use or spending period, two end dates in one norm, a date repeating yearly, or a year the document does not establish.
Output: write 추출_<A|B>.md in the input folder. Return only the counts of provisions, concepts, relations and norms.

Output format: follow the section 출력 형식 of docs/5_룰/5-7b_추출_에이전트_지침.md exactly, and write the output as ontology/추출/도약/2_도약_별지_공통인용/작업_B/추출_B.md.
Keep this session under 300k tokens of context.
