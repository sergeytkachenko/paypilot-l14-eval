export SET GATE CASE PROFILE DEFECTS JUDGE_MODEL JUDGE_BACKEND OLLAMA_URL GATE_MODE TRIAGE_MODEL

.PHONY: eval dry-run compare test release triage heal heal-report
eval:
	docker compose run --rm eval

dry-run:
	docker compose run --rm eval --dry-run

compare:
	docker compose run --rm eval --compare $(A) $(B)

test:
	docker compose run --rm --entrypoint pytest eval tests -q

release:
	docker compose run --rm -e GATE=release -e JUDGE_BACKEND=ollama -e JUDGE_MODEL=qwen2.5:7b eval

triage:
	docker compose run --rm --entrypoint python eval evals/agent/triage.py triage --builds evals/healer/red_builds.jsonl --out evals/healer/triage_result.json

heal:
	docker compose run --rm --entrypoint python eval evals/agent/triage.py heal --builds evals/healer/red_builds.jsonl --triage-json evals/healer/triage_result.json --proposals-dir evals/healer/proposals --out evals/healer/heal_result.json

heal-report:
	docker compose run --rm --entrypoint python eval scripts/heal_report.py --healer-dir evals/healer
