# -*- coding: utf-8 -*-
"""어휘 정의 채점 — 「정의 X 로 붙인 라벨」이 정답셋을 얼마나 재현하는가.

사용: python scratchpad/어휘채점.py 어휘_라벨_A.json
라벨 파일 형식: {"정의": "현행B0"|"신정의", "라벨": {"368": "불가", ...}}
두 정의를 한 파일에 낼 수도 있다: {"현행B0": {...}, "신정의": {...}}
"""
import sys, os, json
from collections import Counter

라벨값 = ("가능", "조건부", "불가", "판단불가", "선택필요")
여기 = os.path.dirname(os.path.abspath(__file__))


def 열기(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def 채점(붙인, 키, 이름):
    맞음 = 틀림 = 미제출 = 0
    혼동 = Counter()
    이상 = []
    for gid, 정답 in 키.items():
        내답 = 붙인.get(gid) or 붙인.get(str(gid))
        if not 내답:
            미제출 += 1
            continue
        if 내답 not in 라벨값:
            이상.append((gid, 내답))
            continue
        혼동[(정답, 내답)] += 1
        if 내답 == 정답:
            맞음 += 1
        else:
            틀림 += 1
    총 = 맞음 + 틀림
    print(f"\n━━ {이름}")
    if 이상:
        print(f"  🔴 라벨값이 아님 {len(이상)}건: {이상[:4]}")
    if 미제출:
        print(f"  ⚠️ 미제출 {미제출}건 — 통과로 세지 않는다")
    print(f"  재현율 {맞음}/{총} = {100*맞음/총:.1f}%" if 총 else "  채점 불가")
    정답들 = sorted({k[0] for k in 혼동}, key=lambda x: -sum(v for k, v in 혼동.items() if k[0] == x))
    예측들 = sorted({k[1] for k in 혼동})
    print("  %-9s" % "정답\붙임" + "".join("%9s" % p for p in 예측들))
    for a in 정답들:
        print("  %-9s" % a + "".join("%9d" % 혼동.get((a, p), 0) for p in 예측들))
    return 맞음, 총, 혼동


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    p = sys.argv[1]
    레 = os.path.basename(p).rsplit("_", 1)[-1][0]
    키 = 열기(os.path.join(여기, f"어휘_채점키_{레}.json"))
    자료 = 열기(p)
    결과 = {}
    if "라벨" in 자료:
        결과[자료.get("정의", "무명")] = 자료["라벨"]
    else:
        for k, v in 자료.items():
            if isinstance(v, dict):
                결과[k] = v
    if not 결과:
        print("🔴 라벨을 못 찾았다"); return 2
    점수 = {}
    for 이름, 붙인 in 결과.items():
        m, t, _ = 채점(붙인, 키, 이름)
        점수[이름] = (m, t)
    if len(점수) == 2:
        (n1, (m1, t1)), (n2, (m2, t2)) = list(점수.items())
        print(f"\n━━ 대조  {n1} {100*m1/t1:.1f}%  vs  {n2} {100*m2/t2:.1f}%"
              f"   차 {100*(m2/t2-m1/t1):+.1f}%p  ({m2-m1:+d}문항)")
        if abs(m2 - m1) <= 5:
            print("  🔴 차이 5문항 이하 — 이 슬라이스만으로는 «정의가 낫다» 를 못 말한다")
    print("\n🔴 재현율이 낮다고 정의가 틀린 게 아니다 — 정답셋이 일관되지 않을 수도 있다.")
    print("   틀린 문항의 gold_id 를 반드시 같이 보고해라. 그게 다음 판단의 재료다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
