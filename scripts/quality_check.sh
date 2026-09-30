#!/bin/bash
# ==============================================================================
# AIRS quality gate - invoked by Jenkinsfile.ci (qualityScriptPath).
#
# Same seven checks, same order, as User_Management_System/scripts/quality_check.sh,
# retargeted from Backend/ to app/. Two deliberate differences from that script:
#
#  1. Formatters run in CHECK mode, not --in-place. UMS rewrites its source
#     during CI, but the pipeline calls cleanWs() afterwards, so those edits are
#     discarded - the gate reports success while changing nothing. Checking
#     instead actually reports the problem.
#
#  2. Each gate's blocking behaviour is a variable below, and the ones that
#     CANNOT pass on this repo today ship as warn-only. This is not a way to
#     dodge the gate - it is so adopting CI does not block every pull request
#     on pre-existing debt. Each has a concrete condition for flipping it to 1,
#     stated inline. Flip them as the debt is cleared.
# ==============================================================================

set -uo pipefail

TARGET_DIR="app"
TEST_DIR="tests"
REQUIREMENTS_FILE="requirements.prod.txt"   # audit what ships, not the CI tools
MIN_COVERAGE="${MIN_COVERAGE:-60}"

# ── Which gates block the build ───────────────────────────────────────────────
# 0 = report and continue, 1 = fail the build.

# app/ has never been formatted (376 files); a blocking check fails on the
# first PR with a ~370-file diff. Flip to 1 once `black app/` has been run
# and committed once.
STRICT_FORMAT="${STRICT_FORMAT:-0}"

# Same story for lint - unknown pre-existing flake8 count. Flip to 1 once the
# reported count is 0.
STRICT_LINT="${STRICT_LINT:-0}"

# app/ is untyped; mypy on 376 unannotated files reports thousands of errors.
# Flip to 1 only after the codebase has type annotations worth gating on.
STRICT_TYPES="${STRICT_TYPES:-0}"

# tests/ has NO conftest.py, and its 144 files import TestClient / SessionLocal
# directly - they need a reachable PostgreSQL (with pgvector) and Redis, which
# the CI agent does not provide. Until fixtures or service containers exist,
# a blocking test gate would fail every build. Flip to 1 the moment tests can
# actually run headless. THIS IS THE MOST IMPORTANT ONE TO FIX.
STRICT_TESTS="${STRICT_TESTS:-0}"

# Security gates block from day one - these find real defects, not style debt,
# and neither depends on the codebase being formatted or annotated.
STRICT_SECURITY="${STRICT_SECURITY:-1}"

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
FAILED=0

# Records a gate result. $1 = gate name, $2 = exit code, $3 = strict flag.
report() {
    local name="$1" code="$2" strict="$3"
    if [ "${code}" -eq 0 ]; then
        echo -e "${GREEN}${name} passed${NC}"
    elif [ "${strict}" -eq 1 ]; then
        echo -e "${RED}${name} FAILED (blocking)${NC}"
        FAILED=1
    else
        echo -e "${YELLOW}${name} reported issues (non-blocking - see the STRICT_* notes in this script)${NC}"
    fi
}

echo "Starting AIRS quality checks | target=${TARGET_DIR} tests=${TEST_DIR}"

echo -e "\n[1/7] Autoflake (unused imports)..."
autoflake --check-diff --remove-all-unused-imports --recursive "${TARGET_DIR}"
report "Autoflake" $? "${STRICT_FORMAT}"

echo -e "\n[2/7] Black (formatting)..."
black --check --diff "${TARGET_DIR}"
report "Black" $? "${STRICT_FORMAT}"

echo -e "\n[3/7] Flake8 (lint)..."
flake8 "${TARGET_DIR}" --count --max-line-length=88 --extend-ignore=E203,E501 --statistics
report "Flake8" $? "${STRICT_LINT}"

echo -e "\n[4/7] Bandit (security)..."
bandit -r "${TARGET_DIR}" -ll -ii
report "Bandit" $? "${STRICT_SECURITY}"

echo -e "\n[5/7] pip-audit (dependency CVEs in ${REQUIREMENTS_FILE})..."
pip-audit -r "${REQUIREMENTS_FILE}"
report "pip-audit" $? "${STRICT_SECURITY}"

echo -e "\n[6/7] Mypy (types)..."
mypy "${TARGET_DIR}" --ignore-missing-imports --explicit-package-bases
report "Mypy" $? "${STRICT_TYPES}"

echo -e "\n[7/7] Pytest (coverage gate ${MIN_COVERAGE}%)..."
pytest "${TEST_DIR}" \
    --cov="${TARGET_DIR}" \
    --cov-report=term-missing \
    --cov-fail-under="${MIN_COVERAGE}"
report "Pytest" $? "${STRICT_TESTS}"

echo
if [ "${FAILED}" -eq 0 ]; then
    echo -e "${GREEN}ALL BLOCKING QUALITY GATES PASSED${NC}"
    exit 0
fi
echo -e "${RED}QUALITY GATE FAILED${NC}"
echo -e "${YELLOW}Hint: pytest ${TEST_DIR} --cov=${TARGET_DIR} --cov-report=html shows uncovered lines locally.${NC}"
exit 1
