# pg_restore --list: 명시된 AGE 파생 객체만 제외한다. 다른 그래프·예상 밖 객체는 중단한다.
BEGIN {
    graph=0; catalog=0; views=0; tables=0
    split("_ag_label_vertex _ag_label_edge Entity Norm Alias Source Relation DocArticle RELATION FROM_NODE TO_NODE TARGETS ADDRESSEE APPROVER EXCEPTION_TO EXCEPTION_TEXT HAS_ALIAS HAS_SOURCE IN_ARTICLE", names, " ")
    for (i in names) allowed[names[i]]=1
}
/^;/ { print; next }
/ SCHEMA - suddoe_ontology / { graph++; next }
/^[0-9]+; [0-9]+ [0-9]+ (TABLE DATA|SEQUENCE SET|SEQUENCE OWNED BY|TABLE|VIEW|SEQUENCE|DEFAULT|CONSTRAINT|INDEX|TRIGGER|ACL|COMMENT) suddoe_ontology / {
    if ($4 == "TABLE" && $5 == "suddoe_ontology") {
        if (!allowed[$6] || ++label_seen[$6] != 1) { print "예상 밖 AGE 라벨: 복원 중단" > "/dev/stderr"; exit 1 }
        tables++
    }
    next
}
/ TABLE DATA ag_catalog ag_graph / { if (++graph_data != 1) exit 1; catalog++; next }
/ TABLE DATA ag_catalog ag_label / { if (++label_data != 1) exit 1; catalog++; next }
/ VIEW corpus age_(concepts|concept_labels|concept_relations|norms|provenance) / {
    if (++view_seen[$6] != 1) exit 1
    views++; next
}
/ TABLE DATA corpus ontology_age_state / { next }
/ TABLE .* _ag_label_vertex / { print "다른 AGE 그래프: 복원 중단" > "/dev/stderr"; exit 1 }
/ suddoe_ontology / { print "예상 밖 그래프 관련 객체: 복원 중단" > "/dev/stderr"; exit 1 }
{ print }
END {
    if (graph != 1 || catalog != 2 || views != 5 || tables != 19) {
        print "AGE 덤프 목록이 예상과 다릅니다" > "/dev/stderr"; exit 1
    }
}
