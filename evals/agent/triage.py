import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HYPOTHESES = ("real_defect", "stale_case", "flaky", "environment")
ACTIONS = ("fix_system", "fix_case", "move_layer", "rerun")
REQUIRED_ACTIONS = {
    "flaky": "move_layer",
    "environment": "rerun",
    "stale_case": "fix_case",
    "real_defect": "fix_system",
}
FORBIDDEN_PATHS = (
    "sets/",
    "evals/sets/",
    "specs/",
    "docs/adr",
    "ADR-",
    "red_builds.jsonl",
    "docs/gate-policy.md",
    "docs/self-healing-policy.md",
)

SYSTEM_TRIAGE = """You triage a failing CI check for an LLM quality gate.

Classify the failure into exactly one hypothesis:
  real_defect  the system behaviour is wrong; the case and oracle are right
  stale_case   the system matches current corpus/spec; the test case expects outdated text
  flaky        the case is probabilistic (runs > 1 or intermittent across repeats), not suitable for daily deterministic gate
  environment  the run failed due to missing API key, network/stand error, or unavailable judge

Non-negotiable classification rules:
- If an independent engine oracle (`engine_says`) or tool boolean contradicts the system/tool result -> `real_defect` (even if PR text claims a policy change).
- If `status` is `error`, `assertion` is `stand_error` or `judge_error`, or error mentions missing API key / unreachable service -> `environment`.
- If the case has `runs > 1` with intermittent pass/fail across repeats -> `flaky`.
- If `oracle` is `corpus` and the system answer matches the corpus while the case assertion expects an old value -> `stale_case`.

Action mapping:
- environment -> rerun
- flaky       -> move_layer (NEVER rerun)
- stale_case  -> fix_case
- real_defect -> fix_system

Reply with ONE line of JSON and nothing else:
{"hypothesis": "...", "action": "fix_system|fix_case|move_layer|rerun", "evidence": "<exact field/value quoted>", "confidence": "high|low"}"""

SYSTEM_HEAL = """You propose the smallest fix for a failing CI check. You NEVER apply changes directly.

Hard boundary — you may NOT propose edits to:
  sets/** or evals/sets/**
  specs/**
  docs/adr/** or ADR-*
  red_builds.jsonl
If fixing the failure requires editing any protected path (or if the test case expectation is the problem), set "needs_human": true.

Reply with ONE line of JSON and nothing else:
{"target": "<path or component>", "change": "<one concise sentence>", "diff_preview": "<short unified diff sketch>", "needs_human": true|false, "risk": "<what could break>"}"""

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _default_model() -> str:
    if os.environ.get("TRIAGE_MODEL"):
        return os.environ["TRIAGE_MODEL"]
    if os.environ.get("OPENROUTER_API_KEY"):
        return "anthropic/claude-haiku-5.5"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "claude-haiku-5-5"
    return "gpt-5-mini"


def _ask(system: str, prompt: str) -> tuple[str, str]:
    model = _default_model()
    or_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    ant_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    oai_key = os.environ.get("OPENAI_API_KEY", "").strip()

    if or_key and ("/" in model or not ant_key):
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {or_key}"}
        payload = json.dumps({
            "model": model,
            "max_tokens": 800,
            "temperature": 0,
            "reasoning": {"enabled": False},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        }).encode("utf-8")
        mode = "openai"
    elif ant_key:
        url = "https://api.anthropic.com/v1/messages"
        headers = {"Content-Type": "application/json", "x-api-key": ant_key, "anthropic-version": "2023-06-01"}
        payload = json.dumps({
            "model": model,
            "max_tokens": 800,
            "temperature": 0,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        mode = "anthropic"
    elif oai_key:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {oai_key}"}
        payload = json.dumps({
            "model": model,
            "max_tokens": 800,
            "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        }).encode("utf-8")
        mode = "openai"
    else:
        raise RuntimeError("No LLM API key set (OPENROUTER_API_KEY, ANTHROPIC_API_KEY, or OPENAI_API_KEY)")

    req = urllib.request.Request(url, data=payload, headers=headers)
    delay = 2.0
    for attempt in range(4):
        try:
            with _opener.open(req, timeout=60) as r:
                data = json.loads(r.read().decode("utf-8"))
                if mode == "anthropic":
                    text = "".join(b.get("text", "") for b in data.get("content", []))
                else:
                    text = data["choices"][0]["message"].get("content") or ""
                if text.strip():
                    return text, model
                if attempt < 3:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise RuntimeError(f"triage returned empty response: {str(data)[:200]}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8")[:200]
            if e.code in (429, 500, 502, 503, 529) and attempt < 3:
                time.sleep(delay)
                delay *= 2
                continue
            raise RuntimeError(f"triage {e.code}: {detail}") from e
    raise RuntimeError("triage agent unreachable")


def _json_line(raw: str) -> dict:
    match = re.search(r"\{.*\}", raw or "", re.S)
    if not match:
        raise ValueError(f"agent returned no JSON: {raw[:160]!r}")
    return json.loads(match.group(0))


def _load_report(report_path: Path) -> dict:
    return json.loads(report_path.read_text(encoding="utf-8"))


def _build_context(build: dict) -> str:
    report_rel = build.get("report")
    report_data = {}
    if report_rel:
        rp = (ROOT / report_rel) if not Path(report_rel).is_absolute() else Path(report_rel)
        if rp.exists():
            report_data = _load_report(rp)

    failing_cases = []
    traces = []
    for c in report_data.get("cases", []):
        if not c.get("passed") or c.get("status") in ("fail", "error"):
            failing_cases.append({
                "id": c.get("id"),
                "set": c.get("set"),
                "gate": c.get("gate"),
                "status": c.get("status"),
                "assertion": c.get("assertion"),
                "detail": c.get("detail"),
                "answer": c.get("answer"),
            })
            tf = c.get("trace_file")
            if tf:
                tp = (ROOT / tf) if not Path(tf).is_absolute() else Path(tf)
                if not tp.exists():
                    tp = rp.parent / tf.removeprefix("reports/")
                if tp.exists():
                    td = json.loads(tp.read_text(encoding="utf-8"))
                    traces.append({
                        "case_id": c.get("id"),
                        "tool_calls": td.get("tool_calls", []),
                    })

    summary = {
        "build_id": build.get("build_id"),
        "case_id": build.get("case_id"),
        "failure_detail": build.get("failure_detail"),
        "evidence": build.get("evidence"),
        "pr_context": build.get("pr_context"),
        "report_meta": {
            k: report_data.get(k)
            for k in ("prompt_version", "set_hash", "profile", "judge_model", "rubric_version", "elapsed", "errors", "failed")
            if report_data
        },
        "failing_cases": failing_cases,
        "traces": traces,
    }
    return json.dumps(summary, ensure_ascii=False, indent=2)


def _is_forbidden(target: str) -> bool:
    t = (target or "").lower()
    return any(p.lower() in t for p in FORBIDDEN_PATHS)


def triage_one(build: dict) -> tuple[dict, str]:
    raw, model = _ask(SYSTEM_TRIAGE, _build_context(build))
    out = _json_line(raw)
    hyp = out.get("hypothesis")
    if hyp not in HYPOTHESES:
        raise ValueError(f"unknown hypothesis {hyp!r}")
    if out.get("action") not in ACTIONS:
        raise ValueError(f"unknown action {out.get('action')!r}")
    expected = REQUIRED_ACTIONS[hyp]
    if out["action"] != expected:
        out["action_overridden_from"] = out["action"]
        out["action"] = expected
    return out, model


def heal_one(build: dict, hypothesis: str) -> tuple[dict, str]:
    prompt = f"TRIAGE HYPOTHESIS: {hypothesis}\n\n{_build_context(build)}"
    raw, model = _ask(SYSTEM_HEAL, prompt)
    out = _json_line(raw)
    target = str(out.get("target") or "")
    blocked = _is_forbidden(target)
    out["touches_surface"] = "evaluator" if blocked else "system"
    out["blocked_by_boundary"] = blocked
    if blocked:
        out["needs_human"] = True
    return out, model


def _load_builds(path: Path) -> list[dict]:
    builds = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("//"):
            builds.append(json.loads(line))
    return builds


def _write_proposal_files(out_dir: Path, build_id: str, item: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{build_id}.json").write_text(
        json.dumps(item, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    prop = item["proposal"]
    md = [
        f"# Proposal {build_id}",
        "",
        f"- **Case:** `{item.get('case_id')}`",
        f"- **Triage hypothesis:** `{item.get('hypothesis')}` (`{item.get('action')}`)",
        f"- **Target:** `{prop.get('target')}`",
        f"- **Surface:** `{prop.get('touches_surface')}`",
        f"- **Needs human:** `{prop.get('needs_human')}`",
        f"- **Blocked by boundary:** `{prop.get('blocked_by_boundary')}`",
        f"- **Change:** {prop.get('change')}",
        f"- **Risk:** {prop.get('risk')}",
        "",
        "```diff",
        str(prop.get("diff_preview") or ""),
        "```",
        "",
    ]
    (out_dir / f"{build_id}.md").write_text("\n".join(md), encoding="utf-8")


def cmd_review(args) -> int:
    report_path = Path(args.report)
    if not report_path.exists():
        raise FileNotFoundError(f"Report not found: {report_path}")
    report = _load_report(report_path)
    failing = [c for c in report.get("cases", []) if not c.get("passed") or c.get("status") in ("fail", "error")]
    first_id = failing[0]["id"] if failing else "unit_tests"
    detail = "; ".join(f"{c['id']}: {c.get('detail')}" for c in failing[:3]) or "L11 unit tests failed"
    build = {
        "build_id": args.run_id or report_path.stem,
        "case_id": first_id,
        "report": str(report_path),
        "failure_detail": detail,
        "evidence": {
            "profile": report.get("profile"),
            "set_hash": report.get("set_hash"),
            "prompt_version": report.get("prompt_version"),
            "errors": report.get("errors", 0),
        },
        "pr_context": args.pr_context or "",
    }
    tri, model = triage_one(build)
    heal_prop, _ = heal_one(build, tri["hypothesis"])
    artifact_url = args.artifact_url or "eval-report artifact"
    lines = [
        "### CI Triage Review (read-only)",
        "",
        f"- **Hypothesis:** `{tri['hypothesis']}` (action: `{tri['action']}`, confidence: `{tri['confidence']}`)",
        f"- **Evidence:** {tri['evidence']}",
        f"- **Report metadata:** `prompt_version={report.get('prompt_version')}`, `set_hash={report.get('set_hash')}`, `profile={report.get('profile')}`, `judge_model={report.get('judge_model')}`, `rubric_version={report.get('rubric_version')}`, `elapsed={report.get('elapsed')}s`",
        f"- **Suggested target (not applied):** `{heal_prop.get('target')}` (`needs_human={heal_prop.get('needs_human')}`, `blocked_by_boundary={heal_prop.get('blocked_by_boundary')}`)",
        f"- **Proposed change:** {heal_prop.get('change')}",
        f"- **Artifact:** {artifact_url}",
        f"- **Model:** `{model}` · *Agent permissions: `contents: read`, `pull-requests: write` — no repository writes performed.*",
    ]
    out_md = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).write_text(out_md, encoding="utf-8")
    print(out_md)
    return 0


def cmd_triage(args) -> int:
    builds = _load_builds(Path(args.builds))
    out_rows = []
    for b in builds:
        tri, model = triage_one(b)
        out_rows.append({
            "build_id": b["build_id"],
            "case_id": b.get("case_id"),
            "label": b.get("label"),
            "correct_action": b.get("correct_action"),
            "agent_hypothesis": tri["hypothesis"],
            "agent_action": tri["action"],
            "evidence": tri["evidence"],
            "confidence": tri["confidence"],
            "agrees": tri["hypothesis"] == b.get("label"),
            "action_agrees": tri["action"] == b.get("correct_action"),
            "model": model,
        })
        print(f"{b['build_id']}  label={b.get('label')}  agent={tri['hypothesis']}/{tri['action']}  match={tri['hypothesis'] == b.get('label')}")
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out_rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


def cmd_heal(args) -> int:
    builds = _load_builds(Path(args.builds))
    triage_path = Path(args.triage_json)
    tri_by_id = {}
    if triage_path.exists():
        tri_by_id = {r["build_id"]: r for r in json.loads(triage_path.read_text(encoding="utf-8"))}
    out_rows = []
    prop_dir = Path(args.proposals_dir)
    for b in builds:
        bid = b["build_id"]
        if bid in tri_by_id:
            hyp = tri_by_id[bid]["agent_hypothesis"]
            act = tri_by_id[bid]["agent_action"]
            model = tri_by_id[bid].get("model", _default_model())
        else:
            tri, model = triage_one(b)
            hyp, act = tri["hypothesis"], tri["action"]
        prop, model = heal_one(b, hyp)
        item = {
            "build_id": bid,
            "case_id": b.get("case_id"),
            "label": b.get("label"),
            "correct_action": b.get("correct_action"),
            "hypothesis": hyp,
            "action": act,
            "proposal": prop,
            "model": model,
        }
        out_rows.append(item)
        _write_proposal_files(prop_dir, bid, item)
        print(f"{bid}  target={prop['target']}  surface={prop['touches_surface']}  needs_human={prop['needs_human']}  blocked={prop['blocked_by_boundary']}")
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out_rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="triage.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("review")
    pr.add_argument("--report", required=True)
    pr.add_argument("--run-id", default="")
    pr.add_argument("--artifact-url", default="")
    pr.add_argument("--pr-context", default="")
    pr.add_argument("--out", default="")
    pr.set_defaults(func=cmd_review)

    pt = sub.add_parser("triage")
    pt.add_argument("--builds", default="evals/healer/red_builds.jsonl")
    pt.add_argument("--out", default="evals/healer/triage_result.json")
    pt.set_defaults(func=cmd_triage)

    ph = sub.add_parser("heal")
    ph.add_argument("--builds", default="evals/healer/red_builds.jsonl")
    ph.add_argument("--triage-json", default="evals/healer/triage_result.json")
    ph.add_argument("--proposals-dir", default="evals/healer/proposals")
    ph.add_argument("--out", default="evals/healer/heal_result.json")
    ph.set_defaults(func=cmd_heal)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
