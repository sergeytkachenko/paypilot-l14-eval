"""run_eval.py — єдина точка входу контуру оцінювання PayPilot."""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import assertions as A  # noqa: E402
import loader  # noqa: E402

SETS_DIR = HERE / "sets"
REPORTS_DIR = HERE / "reports"
TRACES_DIR = REPORTS_DIR / "traces"
AGENT_SETS = ["l03", "l04", "l05", "l06", "l07", "l08", "l12"]
VERSION_FIELDS = ("prompt_version", "set_hash", "profile", "judge_model",
                  "rubric_version", "elapsed")
CONDITIONS = VERSION_FIELDS[:-1]


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def set_names() -> list[str]:
    raw = env("SET", "all")
    return AGENT_SETS if raw == "all" else [s.strip() for s in raw.split(",") if s.strip()]


def _raw_cases(name: str) -> dict:
    out = {}
    for line in (SETS_DIR / f"{name}.jsonl").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("//"):
            case = json.loads(line)
            out[case["id"]] = case
    return out


def set_hash(names: list[str], ids: list[str]) -> str:
    selected_ids = set(ids)
    selected_cases = {}
    for name in names:
        for case_id, case in _raw_cases(name).items():
            if case_id in selected_ids:
                if case_id in selected_cases:
                    raise ValueError(f"Duplicate case ID: {case_id}")
                selected_cases[case_id] = case
    if set(selected_cases) != selected_ids:
        missing = selected_ids - set(selected_cases)
        raise ValueError(f"Missing cases: {sorted(missing)}")
    canonical = json.dumps(
        [selected_cases[k] for k in sorted(selected_cases)],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def load_cases(names: list[str], gate: str, only: set[str]) -> tuple[list[dict], str]:
    cases, seen = [], set()
    for name in names:
        for case in loader.load(SETS_DIR / f"{name}.jsonl"):
            if case["id"] in seen:
                raise loader.SetError(f"{name}: id {case['id']} is already in another set")
            seen.add(case["id"])
            case["_set"] = name
            cases.append(case)
    if gate:
        cases = [c for c in cases if c["additional_metadata"].get("gate", "daily") == gate]
    if only:
        cases = [c for c in cases if c["id"] in only]
    return cases, set_hash(names, [c["id"] for c in cases])


def calls_planned(case: dict) -> tuple[int, int]:
    meta = case["additional_metadata"]
    runs = int(env("REPEAT") or meta.get("runs", 1))
    turns = sum(1 for t in case.get("turns", []) if t.get("role") == "user") or 1
    judge = runs if meta["assertion"] == "judge" else 0
    return runs * turns, judge


SPENT = {"agent_calls": 0, "cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0}


def _meter(stand):
    chat = stand.chat

    def metered(*args, **kwargs):
        out = chat(*args, **kwargs)
        usage = out.get("usage") or {}
        SPENT["agent_calls"] += 1
        SPENT["cost_usd"] += float(usage.get("cost_usd") or 0)
        SPENT["tokens_in"] += int(usage.get("input_tokens") or 0)
        SPENT["tokens_out"] += int(usage.get("output_tokens") or 0)
        return out
    stand.chat = metered


def collect_oracles(stand, case: dict, run: dict) -> dict:
    meta = case["additional_metadata"]
    context = case["context"]
    customer_id = context["customer_id"]
    transaction_id = context["transaction_id"]
    engine = meta["engine"]
    tool_result = next(
        (
            call["result"]
            for call in stand.tool_calls(run["trace"])
            if call["name"] == "check_dispute_eligibility"
        ),
        None,
    )
    customers = [row for row in stand.state("customers") if row["id"] == customer_id]
    disputes = [row for row in stand.state("disputes") if row["transaction_id"] == transaction_id]
    return {
        "engine:disputes": engine,
        "tool:check_dispute_eligibility": tool_result,
        "state:customers": customers,
        "state:disputes": disputes,
    }


def _save_case_trace(stamp: str, case_id: str, row: dict, trace_obj: dict | None) -> str:
    folder = TRACES_DIR / stamp
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{case_id}.json"
    payload = {
        "case_id": case_id,
        "status": row.get("status"),
        "verdict": row.get("verdict"),
        "tool_calls": row.get("tool_calls", []),
        "answers": row.get("answers", []),
        "trace": trace_obj or {},
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return str(path.relative_to(HERE))


def run_case(stand, runner, case: dict, stamp: str | None = None) -> dict:
    meta = case["additional_metadata"]
    trace_obj = None
    if meta["assertion"] != "joint":
        row = runner.run_case(case)
        row["checks"] = row["verdict"].get("checks", [])
        if row.get("skipped") or row["verdict"].get("assertion") == "stand_error":
            row["status"] = "error"
        elif row["verdict"]["passed"]:
            row["status"] = "pass"
        else:
            row["status"] = "fail"
        if stamp:
            row["trace_file"] = _save_case_trace(stamp, case["id"], row, row.get("_trace"))
        return row

    skipped = False
    run = {}
    try:
        run = runner._one_run(case)
        trace_obj = run.get("trace")
        oracles = collect_oracles(stand, case, run)
        verdict, checks = assert_case(case, run["answer"], oracles)
    except NotImplementedError as e:
        got = locals().get("oracles")
        note = f"; оракули зібрано: {', '.join(got)}" if got else ""
        verdict, checks, skipped = A.Verdict(False, 4, "joint", f"{e}{note}"), [], True
    except stand.StandError as e:
        verdict, checks, run = A.Verdict(False, 1, "stand_error", str(e)), [], {}

    if skipped or verdict.assertion == "stand_error":
        status = "error"
    elif verdict.passed:
        status = "pass"
    else:
        status = "fail"

    row = {
        "id": case["id"], "layer": meta["layer"], "oracle": meta["oracle"],
        "severity": meta["severity"], "gate": meta.get("gate", "daily"),
        "runs": 1, "skipped": skipped, "status": status, "verdict": verdict.as_dict(),
        "checks": checks, "answers": [run.get("answer", "")[:400]],
        "latency_ms": run.get("latency_ms", 0),
        "tool_calls": stand.tool_calls(run.get("trace") or {}) if run else [],
    }
    if stamp:
        row["trace_file"] = _save_case_trace(stamp, case["id"], row, trace_obj)
    return row


def _check(name: str, level: int, passed: bool, detail: str) -> dict:
    return {"label": name, "level": level, "passed": bool(passed), "detail": detail}


def joint_checks(oracles: dict) -> list[dict]:
    engine = oracles["engine:disputes"]
    tool = oracles["tool:check_dispute_eligibility"]
    customers = oracles["state:customers"]
    disputes = oracles["state:disputes"]
    window_ok = isinstance(tool, dict) and tool.get("checks", {}).get("window") == "pass"
    hold = any(bool(row.get("compliance_hold")) for row in customers)
    engine_denies = engine.get("eligible") is False
    tool_allows = isinstance(tool, dict) and tool.get("eligible") is True
    dispute_exists = bool(disputes)
    cross_ok = not (engine_denies and (tool_allows or dispute_exists))
    return [
        _check("disputes_window_ok", 1, window_ok, "Перевірка вікна спору інструментом"),
        _check("compliance_hold_flag", 1, hold, "Compliance hold у стані клієнта"),
        _check("cross_dispute_under_hold", 4, cross_ok, "Узгодженість рушія, інструмента та стану disputes"),
    ]


def assert_case(case: dict, answer: str, oracles: dict):
    checks = joint_checks(oracles)
    failed = [c for c in checks if not c["passed"]]
    level = max(c["level"] for c in checks)
    detail = "; ".join(f"{c['label']}: {c['detail']}" for c in failed)
    return A.Verdict(not failed, level, "joint", detail or "all checks pass", checks), checks


def unit_tests(dry: bool) -> dict:
    root = Path(env("L11_DIR", "/course/l11"))
    out = {"l11_dir": str(root)}
    course_root = str(Path(__file__).resolve().parents[2])
    proc_env = {**os.environ, "PYTHONPATH": f"{course_root}:{os.environ.get('PYTHONPATH', '')}".strip(":")}
    for name in ("manual", "generated"):
        folder = root / "tests" / name
        if not any(folder.glob("test_*.py")):
            out[name] = {"status": "not found"}
            continue
        args = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"tests/{name}"]
        p = subprocess.run(args + (["--collect-only"] if dry else []), cwd=root,
                           env=proc_env, capture_output=True, text=True)
        lines = [ln for ln in p.stdout.strip().splitlines() if ln.strip()]
        tail = lines[-1] if lines else p.stderr.strip()[-200:]
        num = lambda word: int((re.search(rf"(\d+) {word}", tail) or [0, 0])[1])  # noqa: E731
        if dry:
            out[name] = {"status": "found", "tests": num("tests? collected")}
        else:
            out[name] = {"exit_code": p.returncode, "passed": num("passed"),
                         "failed": num("failed") + num("errors?"), "summary": tail}
    return out


def _unit_ok(block: dict) -> bool:
    return all(isinstance(block.get(k), dict) and block[k].get("exit_code") == 0
               for k in ("manual", "generated"))


def prompt_version(stand) -> tuple[str, str]:
    response = stand._call("GET", "/api/_test/prompt")
    label = response["version"]
    prompt_text = response.get("text", "")
    first_line = prompt_text.splitlines()[0] if prompt_text else ""
    match = re.search(r"\(base\.v\d+\)", first_line)
    version = match.group(0)[1:-1] if match else "unknown"
    overlays = response.get("overlays") or []
    if overlays:
        version += "+" + "+".join(overlays)
    return version, label


def write_record(record: dict, stamp: str | None = None) -> Path:
    REPORTS_DIR.mkdir(exist_ok=True)
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS_DIR / f"run-{stamp}.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    return path


def compare(path_a: str, path_b: str) -> int:
    a = json.loads(Path(path_a).read_text(encoding="utf-8"))
    b = json.loads(Path(path_b).read_text(encoding="utf-8"))
    print("Порівняння прогонів")
    print("-" * 72)
    for field in VERSION_FIELDS:
        left = a.get(field)
        right = b.get(field)
        marker = "DIFF" if left != right else "SAME"
        print(f"{field:<18} {str(left):<20} {str(right):<20} {marker}")
    differences = [field for field in CONDITIONS if a.get(field) != b.get(field)]
    comparable = (
        a.get("set_hash") is not None
        and a["set_hash"] == b.get("set_hash")
        and len(differences) <= 1
    )
    cases_a = {case["id"]: case["passed"] for case in a.get("cases", [])}
    cases_b = {case["id"]: case["passed"] for case in b.get("cases", [])}
    print("\nКейси зі зміненим вердиктом:")
    flipped = [
        case_id
        for case_id in sorted(cases_a.keys() & cases_b.keys())
        if cases_a[case_id] != cases_b[case_id]
    ]
    if flipped:
        for case_id in flipped:
            print(f"  {case_id}: {'PASS' if cases_a[case_id] else 'FAIL'} -> {'PASS' if cases_b[case_id] else 'FAIL'}")
    else:
        print("  Немає змін вердиктів")
    print("\nВисновок:", "COMPARABLE" if comparable else "NOT COMPARABLE")
    return 0 if comparable else 1


def _print_row(case: dict, row: dict) -> None:
    v = row["verdict"]
    status = row.get("status")
    mark = "ERR " if status == "error" else ("pass" if v["passed"] else "FAIL")
    joint = case["additional_metadata"]["assertion"] == "joint"
    detail = "" if joint and row["checks"] else v["detail"]
    print(f"{mark:<5} {case['_set']:<4} {case['id']:<12} {v['assertion']:<22} "
          f"{detail if len(detail) <= 70 else ''}")
    if len(detail) > 70:
        print(f"        {detail}")
    if joint:
        for c in row["checks"]:
            print(f"        {c['label']:<26} {'pass' if c['passed'] else 'FAIL':<5} {c['detail']}")


def dry_run(names, gate, only) -> int:
    cases, h = load_cases(names, gate, only)
    print(f"SET {','.join(names)}  GATE {gate or 'all'}  set_hash {h}")
    print(f"{'набір':<6} {'кейсів':>6} {'daily':>6} {'release':>8} {'агент':>6} {'суддя':>6}")
    total = [0, 0, 0, 0, 0]
    for name in names:
        part = [c for c in cases if c["_set"] == name]
        calls = [calls_planned(c) for c in part]
        nums = [len(part),
                sum(c["additional_metadata"].get("gate", "daily") == "daily" for c in part),
                sum(c["additional_metadata"].get("gate", "daily") == "release" for c in part),
                sum(a for a, _ in calls), sum(j for _, j in calls)]
        total = [x + y for x, y in zip(total, nums)]
        print(f"{name:<6} " + " ".join(f"{n:>{w}}" for n, w in zip(nums, (6, 6, 8, 6, 6))))
    print(f"{'разом':<6} " + " ".join(f"{n:>{w}}" for n, w in zip(total, (6, 6, 8, 6, 6))))
    ut = unit_tests(dry=True)
    print("unit-тести L11: " + ", ".join(
        f"{k} — {v.get('tests', v.get('status'))}" for k, v in ut.items() if k != "l11_dir"))
    if any(isinstance(v, dict) and v.get("status") == "not found" for v in ut.values()):
        print("УВАГА: unit-тести L11 не знайдено — прогін поверне код 1. "
              "Покладіть кіт L11 у ../l11 або задайте L11_DIR у .env")
    if not (Path(__file__).resolve().parents[2] / "app" / "engines").is_dir():
        print("УВАГА: код стенду не знайдено — прогін упаде на першому кейсі. "
              "Покладіть стенд у ../paypilot-stand або задайте STAND_DIR у .env")
    print("нічого не викликано")
    return 0


def main(argv) -> int:
    p = argparse.ArgumentParser(prog="docker compose run --rm eval")
    p.add_argument("--dry-run", action="store_true", help="порахувати кейси й виклики, нічого не викликати")
    p.add_argument("--compare", nargs=2, metavar=("A", "B"), help="покласти два записи поруч")
    args = p.parse_args(argv)
    if args.compare:
        return compare(*args.compare)

    names, gate = set_names(), env("GATE", "daily")
    gate = "" if gate == "all" else gate
    only = {x.strip() for x in env("CASE").split(",") if x.strip()}
    if args.dry_run:
        return dry_run(names, gate, only)

    import stand
    import runner
    from judge import RUBRIC_VERSION
    _meter(stand)
    cases, h = load_cases(names, gate, only)
    if not cases:
        print("no cases selected — check SET, GATE, CASE")
        return 1
    try:
        stand.wait_until_ready()
        stand.set_profile(env("PROFILE", "clean"))
        stand.set_defects(env("DEFECTS") or None)
        info = stand.profile()
        profile = "+".join([info["profile"], *info.get("extra_defects", [])])
        version, label = prompt_version(stand)
    except stand.StandError as e:
        print(e)
        return 2
    judged = any(c["additional_metadata"]["assertion"] == "judge" for c in cases)
    judge_model = (env("JUDGE_MODEL") or "not-set") if judged else "not-used"
    print(f"prompt_version={version}  set_hash={h}  profile={profile}  "
          f"judge_model={judge_model}  rubric_version={RUBRIC_VERSION}  cases={len(cases)}  gate={gate or 'all'}")
    print("-" * 72)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    t0 = time.time()
    rows = []
    try:
        for case in cases:
            row = run_case(stand, runner, case, stamp=stamp)
            row["set"] = case["_set"]
            rows.append(row)
            _print_row(case, row)
    finally:
        stand.set_defects(None)
    units = unit_tests(dry=False)

    elapsed = round(time.time() - t0, 1)
    passed = sum(1 for r in rows if r.get("status") == "pass")
    errors = sum(1 for r in rows if r.get("status") == "error")
    failed = sum(1 for r in rows if r.get("status") == "fail")
    gate_mode = env("GATE_MODE", "full")

    if gate_mode not in ("smoke", "full"):
        raise ValueError(f"Unknown GATE_MODE: {gate_mode}")

    l11_required = gate_mode == "full"
    l11_ok = _unit_ok(units) if l11_required else True
    code = 0 if failed == 0 and errors == 0 and l11_ok else 1

    tokens_total = SPENT["tokens_in"] + SPENT["tokens_out"]
    record = {
        "prompt_version": version, "set_hash": h, "profile": profile,
        "judge_model": judge_model, "rubric_version": RUBRIC_VERSION, "elapsed": elapsed,
        "set": ",".join(names), "gate": gate or "all", "case_filter": sorted(only),
        "stand_prompt_label": label,
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gate_mode": gate_mode,
        "l11_required": l11_required,
        "passed": passed, "failed": failed, "errors": errors,
        "skipped": sum(1 for r in rows if r.get("skipped")), "total": len(rows),
        "machine": {
            "agent_calls": SPENT["agent_calls"],
            "tokens_in": SPENT["tokens_in"],
            "tokens_out": SPENT["tokens_out"],
            "tokens_total": tokens_total,
            "cost_usd": round(SPENT["cost_usd"], 4),
        },
        "unit_tests": units, "exit_code": code,
        "cases": [{"id": r["id"], "set": r["set"], "layer": r["layer"], "gate": r["gate"],
                   "status": r.get("status", "fail"),
                   "assertion": r["verdict"]["assertion"], "passed": r["verdict"]["passed"],
                   "skipped": bool(r.get("skipped")), "level": r["verdict"]["level"],
                   "detail": r["verdict"]["detail"][:300], "checks": r.get("checks", []),
                   "trace_file": r.get("trace_file"),
                   "answer": (r.get("answers") or [""])[-1][:300]} for r in rows],
    }
    path = write_record(record, stamp=stamp)
    print("-" * 72)
    print(f"{passed}/{len(rows)} passed, {failed} failed, {errors} errors"
          f"   elapsed={elapsed}s   calls={SPENT['agent_calls']}"
          f"   tokens={tokens_total}   ${SPENT['cost_usd']:.4f}")
    for k, v in units.items():
        if k != "l11_dir":
            print(f"unit-тести {k}: {v.get('summary', v.get('status'))}")
    print(f"record -> {path.relative_to(HERE)}   exit {code}")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
