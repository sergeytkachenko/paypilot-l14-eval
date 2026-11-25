import argparse
import json
import math
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def wilson(hits: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total == 0:
        return (0.0, 0.0)
    p = hits / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def _is_autonomous(h: dict) -> bool:
    prop = h["proposal"]
    return not prop.get("needs_human") and not prop.get("blocked_by_boundary")


def _touches_evaluator(h: dict) -> bool:
    return h["proposal"].get("touches_surface") == "evaluator"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="heal_report.py")
    p.add_argument("--healer-dir", default="evals/healer")
    args = p.parse_args(argv)
    hdir = Path(args.healer_dir)

    triage = json.loads((hdir / "triage_result.json").read_text(encoding="utf-8"))
    heal = json.loads((hdir / "heal_result.json").read_text(encoding="utf-8"))
    by_id = {h["build_id"]: h for h in heal}

    n = len(triage)
    hyp_hits = sum(1 for r in triage if r["agrees"])
    act_hits = sum(1 for r in triage if r["action_agrees"])
    h_lo, h_hi = wilson(hyp_hits, n)
    a_lo, a_hi = wilson(act_hits, n)

    all_proposals = heal
    auto_proposals = [h for h in heal if _is_autonomous(h)]
    false_heals_auto = [h for h in auto_proposals if h["correct_action"] != "fix_system" or _touches_evaluator(h)]
    false_heals_all = [
        h for h in all_proposals
        if h["label"] == "real_defect" and _touches_evaluator(h)
    ]

    f_auto_lo, f_auto_hi = wilson(len(false_heals_auto), max(1, len(auto_proposals)))
    f_all_lo, f_all_hi = wilson(len(false_heals_all), max(1, len(all_proposals)))
    rate_auto = len(false_heals_auto) / len(auto_proposals) if auto_proposals else 0.0
    rate_all = len(false_heals_all) / len(all_proposals) if all_proposals else 0.0

    rows = {"system": {"real_defect": 0, "other": 0}, "evaluator": {"real_defect": 0, "other": 0}}
    for h in heal:
        col = "evaluator" if _touches_evaluator(h) else "system"
        row = "real_defect" if h["label"] == "real_defect" else "other"
        rows[col][row] += 1

    print("=== Дві цифри (з 95% інтервалом Вілсона) ===")
    print(f"Узгодженість тріажу (гіпотеза): {hyp_hits}/{n} = {pct(hyp_hits / n)} [{pct(h_lo)}; {pct(h_hi)}]")
    print(f"Узгодженість дій:               {act_hits}/{n} = {pct(act_hits / n)} [{pct(a_lo)}; {pct(a_hi)}]")
    print(f"False heal rate (на пропозиціях, що торкаються вимірника при real_defect): "
          f"{len(false_heals_all)}/{len(all_proposals)} = {pct(rate_all)} [{pct(f_all_lo)}; {pct(f_all_hi)}]")
    print(f"False heal rate (автономний контур needs_human=false): "
          f"{len(false_heals_auto)}/{len(auto_proposals)} = {pct(rate_auto)} [{pct(f_auto_lo)}; {pct(f_auto_hi)}]")

    print("\n=== Матриця 2x2 (людська мітка x шлях у diff) ===")
    print(f"{'':<18}{'система (код/промпт)':<24}{'вимірник (sets/specs/gate)'}")
    print(f"{'real_defect':<18}{rows['system']['real_defect']:<24}{rows['evaluator']['real_defect']}")
    print(f"{'not_defect':<18}{rows['system']['other']:<24}{rows['evaluator']['other']}")

    print("\n=== Розклад по гіпотезах ===")
    print(f"{'Мітка':<14}{'n':<4}{'Тріаж OK':<10}{'Дія OK':<8}{'Система':<9}{'Вимірник':<10}{'Blocked'}")
    for hyp in ("environment", "real_defect", "stale_case", "flaky"):
        sub = [r for r in triage if r["label"] == hyp]
        if not sub:
            continue
        ids = {r["build_id"] for r in sub}
        sys_cnt = sum(1 for h in heal if h["build_id"] in ids and not _touches_evaluator(h))
        eval_cnt = sum(1 for h in heal if h["build_id"] in ids and _touches_evaluator(h))
        blk_cnt = sum(1 for h in heal if h["build_id"] in ids and h["proposal"].get("blocked_by_boundary"))
        print(f"{hyp:<14}{len(sub):<4}{sum(1 for r in sub if r['agrees']):<10}"
              f"{sum(1 for r in sub if r['action_agrees']):<8}{sys_cnt:<9}{eval_cnt:<10}{blk_cnt}")

    print("\n=== Промахи ===")
    misses = [r for r in triage if not r["agrees"] or not r["action_agrees"]]
    if not misses:
        print("  Промахів тріажу немає")
    for r in misses:
        print(f"  {r['build_id']}: мітка {r['label']}/{r['correct_action']} vs агент {r['agent_hypothesis']}/{r['agent_action']}")

    score = {
        "cases": n,
        "triage_agreement": {"hits": hyp_hits, "total": n, "pct": round(100 * hyp_hits / n), "ci": [round(100 * h_lo), round(100 * h_hi)]},
        "action_agreement": {"hits": act_hits, "total": n, "pct": round(100 * act_hits / n), "ci": [round(100 * a_lo), round(100 * a_hi)]},
        "false_heal_rate_real_on_eval": {"hits": len(false_heals_all), "total": len(all_proposals), "pct": round(100 * rate_all), "ci": [round(100 * f_all_lo), round(100 * f_all_hi)]},
        "false_heal_rate_auto": {"hits": len(false_heals_auto), "total": len(auto_proposals), "pct": round(100 * rate_auto), "ci": [round(100 * f_auto_lo), round(100 * f_auto_hi)]},
        "matrix": rows,
    }
    (hdir / "score.json").write_text(json.dumps(score, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
