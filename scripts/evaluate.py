"""分诊评测。

    python -m scripts.evaluate
    python -m scripts.evaluate --csv data/triage_eval.csv --out data/eval_result.json

打印 Top-1/Top-3 准确率、模糊主诉的转人工率、置信度阈值扫描表，
以及按科室拆分的错误明细（用来决定该给哪个科补语料）。

阈值扫描表里同时给「自动接诊量」和「接诊准确率」。只看准确率会走偏：
阈值调高，接得少自然更准，但明确主诉被推给人工的比例也在涨。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.triage import TriageService          # noqa: E402

DEFAULT_CSV = ROOT / "data" / "triage_eval.csv"
DEFAULT_OUT = ROOT / "data" / "eval_result.json"

# 扫描的候选阈值。注意 0.0 表示"从不转人工"，作为上界参考。
THRESHOLD_GRID = [0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50, 0.60]


def load_cases(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    cases = []
    for row in rows:
        cases.append({
            "symptom": row["symptom"].strip(),
            "is_child": row["is_child"].strip() == "1",
            "expected": row["expected"].strip(),
            "note": row.get("note", "").strip(),
        })
    return cases


def run(service: TriageService, cases: list[dict], top_k: int = 3) -> list[dict]:
    results = []
    for case in cases:
        r = service.triage(case["symptom"], is_child=case["is_child"], top_k=top_k)
        names = [c["department_name"] for c in r["candidates"]]
        results.append({
            **case,
            "predicted": names[0] if names else "",
            "candidates": names,
            "confidence": r["confidence"],
            "need_human": r["need_human"],
        })
    return results


def summarize(results: list[dict]) -> dict:
    labeled = [r for r in results if r["expected"]]
    negative = [r for r in results if not r["expected"]]

    top1 = sum(1 for r in labeled if r["predicted"] == r["expected"])
    top3 = sum(1 for r in labeled if r["expected"] in r["candidates"])
    auto = [r for r in labeled if not r["need_human"]]
    auto_ok = sum(1 for r in auto if r["predicted"] == r["expected"])
    negative_ok = sum(1 for r in negative if r["need_human"])

    per_dept: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "hit": 0})
    for r in labeled:
        per_dept[r["expected"]]["total"] += 1
        if r["predicted"] == r["expected"]:
            per_dept[r["expected"]]["hit"] += 1

    return {
        "labeled_total": len(labeled),
        "negative_total": len(negative),
        "top1": top1,
        "top1_rate": round(top1 / len(labeled), 4) if labeled else 0.0,
        "top3": top3,
        "top3_rate": round(top3 / len(labeled), 4) if labeled else 0.0,
        "auto_count": len(auto),
        "auto_accuracy": round(auto_ok / len(auto), 4) if auto else 0.0,
        "negative_routed_to_human": negative_ok,
        "negative_human_rate": round(negative_ok / len(negative), 4) if negative else 0.0,
        "per_department": {k: dict(v) for k, v in sorted(per_dept.items())},
    }


def sweep(results: list[dict]) -> list[dict]:
    """按候选阈值重算 need_human，看准确率与自动接诊量的权衡。"""
    labeled = [r for r in results if r["expected"]]
    negative = [r for r in results if not r["expected"]]
    rows = []
    for th in THRESHOLD_GRID:
        auto = [r for r in labeled if r["confidence"] >= th]
        auto_ok = sum(1 for r in auto if r["predicted"] == r["expected"])
        neg_ok = sum(1 for r in negative if r["confidence"] < th)
        rows.append({
            "threshold": th,
            "auto_count": len(auto),
            "auto_accuracy": round(auto_ok / len(auto), 4) if auto else 0.0,
            "missed": len(labeled) - len(auto),          # 被推给人工的明确样本（漏诊）
            "negative_caught": neg_ok,                   # 被正确拦下的模糊样本
            "negative_total": len(negative),
        })
    return rows


def print_report(summary: dict, rows: list[dict], results: list[dict]) -> None:
    print()
    print("=" * 74)
    print("分诊效果评测报告")
    print("=" * 74)
    print(f"样本总数        ：{summary['labeled_total'] + summary['negative_total']}"
          f"（明确科室 {summary['labeled_total']} + 模糊主诉 {summary['negative_total']}）")
    print(f"Top-1 准确率    ：{summary['top1']}/{summary['labeled_total']} "
          f"= {summary['top1_rate']:.1%}")
    print(f"Top-3 准确率    ：{summary['top3']}/{summary['labeled_total']} "
          f"= {summary['top3_rate']:.1%}")
    print(f"自动接诊量      ：{summary['auto_count']}，其中判对 "
          f"{summary['auto_accuracy']:.1%}")
    print(f"模糊主诉拦下    ：{summary['negative_routed_to_human']}/"
          f"{summary['negative_total']} = {summary['negative_human_rate']:.1%}")

    print()
    print("-" * 74)
    print("置信度阈值扫描（选阈值的依据）")
    print("-" * 74)
    print(f"{'阈值':>6} {'自动接诊':>9} {'接诊准确率':>11} {'漏给人工':>9} {'拦下模糊':>9}")
    for r in rows:
        print(f"{r['threshold']:>6.2f} {r['auto_count']:>9} "
              f"{r['auto_accuracy']:>10.1%} {r['missed']:>9} "
              f"{r['negative_caught']:>5}/{r['negative_total']}")

    print()
    print("-" * 74)
    print("按科室拆分（找该补语料的科室）")
    print("-" * 74)
    for dept, stat in sorted(summary["per_department"].items(),
                             key=lambda kv: kv[1]["hit"] / kv[1]["total"]):
        flag = "  <-- 需要补语料" if stat["hit"] / stat["total"] < 0.8 else ""
        print(f"   {dept:<10} {stat['hit']}/{stat['total']}{flag}")

    wrong = [r for r in results if r["expected"] and r["predicted"] != r["expected"]]
    if wrong:
        print()
        print("-" * 74)
        print(f"判错的样本（{len(wrong)} 条）")
        print("-" * 74)
        for r in wrong:
            print(f"   {r['symptom']}")
            print(f"      期望 {r['expected']} / 实际 {r['predicted']} "
                  f"/ 候选 {r['candidates']} / 覆盖率 {r['confidence']}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="分诊效果评测")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--top-k", type=int, default=3)
    args = parser.parse_args()

    if not args.csv.exists():
        print(f"评测集不存在：{args.csv}")
        return 1

    cases = load_cases(args.csv)
    service = TriageService()
    results = run(service, cases, top_k=args.top_k)
    summary = summarize(results)
    rows = sweep(results)

    print_report(summary, rows, results)

    payload = {
        "eval_file": str(args.csv.relative_to(ROOT)),
        "mode": service.settings.triage_mode,
        "top_k": args.top_k,
        "summary": summary,
        "threshold_sweep": rows,
        "errors": [
            {"symptom": r["symptom"], "expected": r["expected"],
             "predicted": r["predicted"], "candidates": r["candidates"],
             "confidence": r["confidence"]}
            for r in results if r["expected"] and r["predicted"] != r["expected"]
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果已写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
