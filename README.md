# PayPilot Agentic CI & Self-Healing Kit (L14)

Кіт лабораторної L14 («Agentic CI/CD: тріаж червоних білдів і Self-Healing Policy») поверх гейта L13 (`paypilot-l13-eval`).

## Швидкий старт локально

Поруч із цим каталогом мають лежати:
- `../paypilot-stand` (`sergeytkachenko/paypilot-stand-shared`, коміт `74f1995`)
- `../l11` (`sergeytkachenko/paypilot-l11-eval` або тести з `tests_l11_reference/`)

```bash
cp .env.example .env
docker compose run --rm --entrypoint pytest eval tests -q
docker compose run --rm eval --dry-run
```

## Інструменти L14

- `evals/agent/triage.py` — CI-агент із трьома режимами:
  - `review`: читає артефакт `eval-report` (`reports/run-*.json` + `reports/traces/`) і формує коментар у PR із гіпотезою та доказом (токен `contents: read`, `pull-requests: write`).
  - `triage`: класифікує набір червоних білдів `evals/healer/red_builds.jsonl` на 4 гіпотези (`real_defect`, `stale_case`, `flaky`, `environment`) і 4 дії (`fix_system`, `fix_case`, `move_layer`, `rerun`).
  - `heal`: пропонує мінімальну правку у `evals/healer/proposals/<build_id>.md` та `.json`, але ніколи не застосовує її і блокує зміни в `sets/`, `evals/sets/`, `specs/`, `docs/adr/`, `red_builds.jsonl`.
- `scripts/heal_report.py` — рахує узгодженість тріажу та false heal rate з 95% довірчими інтервалами Вілсона, матрицю 2×2 і розклад по гіпотезах.
- `Makefile`: `make triage`, `make heal`, `make heal-report` (або `docker compose run --rm --entrypoint python eval ...`).
- `evals/healer_reference/` — запасний еталонний набір (10 розмічених білдів, пропозиції, звіт і зразок політики).
- `ci-agent-self-healing.template.md` — шаблон артефакту L14 (Частина A → `evals/healer/heal_report.md`, Частина B → `docs/self-healing-policy.md`).
