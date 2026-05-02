#!/bin/bash
# Phase 9.4 Test Runner: Builder Retry with Analyst Context and Chronicle Hints
#
# Captures full telemetry for narrative reconstruction:
#   --emit-raw: Manager LLM responses
#   --emit-subagent-raw: Subagent returns
#   --emit-board: Manager board view each turn
#
# Usage: ./run_phase94.sh [session_path]
# Default: /tmp/a7rt-phase94

set -e

SESSION_PATH="${1:-/tmp/a7rt-phase94}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEST_DIR="$REPO_ROOT/tests"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RESULTS_DIR="$SESSION_PATH/phase94_results_$TIMESTAMP"

echo "========================================"
echo "Phase 9.4 Test: Builder Retry Context"
echo "========================================"
echo "Session: $SESSION_PATH"
echo "Results: $RESULTS_DIR"
echo ""

# Create results directory
mkdir -p "$RESULTS_DIR"

# Verify session exists
if [ ! -f "$SESSION_PATH/master.json" ]; then
    echo "ERROR: Session not found at $SESSION_PATH"
    echo "Create with: python -m tui init $SESSION_PATH --yes"
    exit 1
fi

echo "[1] Session verified: $SESSION_PATH"

# Check for required nodes
if ! python3 -c "
import json
import sys
with open('$SESSION_PATH/master.json') as f:
    d = json.load(f)
    nodes = list(d.get('nodes', {}).keys())
    required = ['jwt_util', 'auth_handler', 'auth_test']
    missing = [n for n in required if n not in nodes]
    if missing:
        print(f'Missing nodes: {missing}')
        sys.exit(1)
    print(f'Nodes: {nodes}')
" 2>/dev/null; then
    echo "ERROR: Required nodes not found. Seed with:"
    echo "  python -m tui seed $SESSION_PATH --nodes '{\"id\":\"jwt_util\",...}'"
    exit 1
fi

echo "[2] Nodes verified: jwt_util, auth_handler, auth_test"

# Run harness with telemetry capture
echo ""
echo "[3] Running harness with full telemetry..."
echo "    Output: $RESULTS_DIR/events.jsonl"
echo "    Telemetry: $RESULTS_DIR/telemetry.jsonl"
echo ""

cd "$REPO_ROOT"

python3 -c "
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path('$REPO_ROOT')))

from harness import Harness, HaltSignal
from repository import Repository
from models import ManagerState

# Import test hooks
from tests.phase94_manager_hook import phase94_manager_hook
from tests.phase94_subagent_hook import phase94_subagent_hook

# Initialize
repo = Repository(Path('$SESSION_PATH'))

# Get stage_id from master
doc = repo._load()
stage_id = list(doc.get('stages', {}).keys())[0]
print(f'Using stage: {stage_id}', file=sys.stderr)

# Create harness
harness = Harness(
    repo=repo,
    manager_hook=phase94_manager_hook,
    subagent_hook=phase94_subagent_hook,
)

# Run session
print('Starting phase 9.4 test run...', file=sys.stderr)
try:
    harness.run_session(stage_id)
    print('Session completed normally', file=sys.stderr)
except HaltSignal as e:
    print(f'Session halted: {e}', file=sys.stderr)
except Exception as e:
    print(f'Session error: {e}', file=sys.stderr)
    import traceback
    traceback.print_exc()
    sys.exit(1)

print('Run complete', file=sys.stderr)
" 2>"$RESULTS_DIR/telemetry.jsonl" | tee "$RESULTS_DIR/events.jsonl"

EXIT_CODE=${PIPESTATUS[0]}

echo ""
echo "[4] Run complete (exit: $EXIT_CODE)"

# Verify retry context
echo ""
echo "[5] Verifying retry context..."
python3 "$TEST_DIR/phase94_manager_hook.py" "$SESSION_PATH" | tee "$RESULTS_DIR/verification.json"

# Extract key metrics
echo ""
echo "[6] Extracting metrics..."

python3 -c "
import json
import sys

# Load verification
with open('$RESULTS_DIR/verification.json') as f:
    v = json.load(f)

print('Phase 9.4 Verification Results')
print('=' * 40)
print(f'Overall: {\"PASS\" if v[\"passed\"] else \"FAIL\"}')
print()
print('Checks:')
for check, value in v['checks'].items():
    status = '✓' if value else '✗'
    print(f'  {status} {check}: {value}')
" 2>/dev/null || echo "Verification parse error"

# Generate narrative
echo ""
echo "[7] Generating narrative..."
python3 -m tui narrative "$SESSION_PATH" --max-turns=20 > "$RESULTS_DIR/narrative.txt" 2>/dev/null || echo "Narrative generation skipped"

# Summary
echo ""
echo "========================================"
echo "Phase 9.4 Test Complete"
echo "========================================"
echo "Results directory: $RESULTS_DIR"
echo ""
echo "Files:"
ls -la "$RESULTS_DIR"
echo ""
echo "To inspect state:"
echo "  python3 -m tui inspect $SESSION_PATH"
echo "  python3 $TEST_DIR/phase94_manager_hook.py $SESSION_PATH"
