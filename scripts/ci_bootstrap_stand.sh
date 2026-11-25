#!/usr/bin/env bash
set -euo pipefail

STAND_REPO="${STAND_REPO:-https://github.com/sergeytkachenko/paypilot-stand-shared.git}"
L11_REPO="${L11_REPO:-https://github.com/sergeytkachenko/paypilot-l11-eval.git}"
STAND_PATH="${STAND_DIR:-../paypilot-stand}"
L11_PATH="${L11_DIR:-../l11}"

if [ ! -d "$STAND_PATH/.git" ]; then
  git clone --depth 1 "$STAND_REPO" "$STAND_PATH"
fi

if [ ! -d "$L11_PATH" ]; then
  git clone --depth 1 "$L11_REPO" "$L11_PATH" || mkdir -p "$L11_PATH/tests/manual" "$L11_PATH/tests/generated"
fi

if [ -d "tests_l11_reference" ]; then
  if [ -z "$(find "$L11_PATH/tests/manual" -name 'test_*.py' 2>/dev/null)" ]; then
    mkdir -p "$L11_PATH/tests/manual"
    cp tests_l11_reference/manual/test_*.py "$L11_PATH/tests/manual/"
  fi
  if [ -z "$(find "$L11_PATH/tests/generated" -name 'test_*.py' 2>/dev/null)" ]; then
    mkdir -p "$L11_PATH/tests/generated"
    cp tests_l11_reference/generated/test_*.py "$L11_PATH/tests/generated/"
  fi
fi

if [ -n "${OPENROUTER_API_KEY:-}" ]; then
  LLM_PROVIDER="${LLM_PROVIDER:-openrouter}"
  LLM_MODEL="${LLM_MODEL:-anthropic/claude-haiku-5.5}"
elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  LLM_PROVIDER="${LLM_PROVIDER:-anthropic}"
  LLM_MODEL="${LLM_MODEL:-claude-haiku-5-5}"
elif [ -n "${OPENAI_API_KEY:-}" ]; then
  LLM_PROVIDER="${LLM_PROVIDER:-openai}"
  LLM_MODEL="${LLM_MODEL:-gpt-5-mini}"
else
  LLM_PROVIDER="${LLM_PROVIDER:-mock}"
  LLM_MODEL="${LLM_MODEL:-}"
fi

cat > "$STAND_PATH/.env" <<EOF
PROFILE=${PROFILE:-clean}
DEFECTS=
CLOCK_OVERRIDE=${CLOCK_OVERRIDE:-2026-09-15T10:00:00Z}
LLM_PROVIDER=${LLM_PROVIDER}
LLM_MODEL=${LLM_MODEL}
OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-}
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}
OPENAI_API_KEY=${OPENAI_API_KEY:-}
EOF

docker compose -f "$STAND_PATH/docker-compose.yml" --project-directory "$STAND_PATH" up -d --build stand

for attempt in $(seq 1 40); do
  if curl -fsS http://localhost:8000/health >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

curl -fsS http://localhost:8000/health
echo ""
docker exec paypilot-stand-stand-1 python scripts/doctor.py || true
