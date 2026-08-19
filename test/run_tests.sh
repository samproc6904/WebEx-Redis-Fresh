#!/bin/bash
# Run all tests for the Webex project
# Usage: bash test/run_tests.sh [--quick]

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BACKEND_DIR="$PROJECT_DIR/backend"

cd "$PROJECT_DIR"

echo "╔══════════════════════════════════════╗"
echo "║      Webex Test Suite                 ║"
echo "╚══════════════════════════════════════╝"
echo ""

# Check server is running
echo "▶ Checking server..."
if ! curl -s http://localhost:8000/api/ping > /dev/null 2>&1; then
    echo "  ✗ Server not running on localhost:8000"
    echo "  Start server first: cd backend && .venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000"
    exit 1
fi
echo "  ✓ Server is running"
echo ""

# Quick mode: only fast tests
if [ "$1" = "--quick" ]; then
    echo "▶ Running quick tests (no API calls)..."
    cd "$BACKEND_DIR"
    .venv/bin/python -m pytest "$SCRIPT_DIR/test_shopify.py::TestClassify" "$SCRIPT_DIR/test_shopify.py::TestParseCard" "$SCRIPT_DIR/test_shopify.py::TestRandomSite" -v --tb=short
    echo ""
    echo "✓ Quick tests passed!"
    exit 0
fi

# Run all tests
echo "▶ Running shopify checker tests..."
cd "$BACKEND_DIR"
.venv/bin/python -m pytest "$SCRIPT_DIR/test_shopify.py" -v --tb=short 2>&1 | tail -20
echo ""

echo "▶ Running backend API tests..."
.venv/bin/python -m pytest "$SCRIPT_DIR/test_backend.py" -v --tb=short 2>&1 | tail -20
echo ""

echo "▶ Running engine tests (may take 2-3 minutes)..."
.venv/bin/python -m pytest "$SCRIPT_DIR/test_engine.py::TestEngineLifecycle::test_start_and_finish" -v --tb=short 2>&1 | tail -10
echo ""

echo "▶ Running integration tests (may take 3-4 minutes)..."
.venv/bin/python -m pytest "$SCRIPT_DIR/test_integration.py::TestFullFlow::test_start_poll_stop" -v --tb=short 2>&1 | tail -10
echo ""

echo "╔══════════════════════════════════════╗"
echo "║      All Tests Complete!              ║"
echo "╚══════════════════════════════════════╝"
