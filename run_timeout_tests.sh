#!/bin/bash
#
# Run timeout mechanism tests
#
# Usage:
#   ./run_timeout_tests.sh              # Run all timeout tests
#   ./run_timeout_tests.sh -v           # Verbose output
#   ./run_timeout_tests.sh --coverage   # With coverage report
#

set -e

echo "========================================"
echo "Markdown Worker Timeout Tests"
echo "========================================"
echo ""

# Check if pytest is installed
if ! python3 -m pytest --version &>/dev/null; then
    echo "❌ pytest not found. Installing test dependencies..."
    pip3 install -r tests/requirements-test.txt
    echo "✅ Test dependencies installed"
    echo ""
fi

# Parse arguments
VERBOSE=""
COVERAGE=""
TEST_FILTER=""

while [[ $# -gt 0 ]]; do
    case $1 in
        -v|--verbose)
            VERBOSE="-v -s"
            shift
            ;;
        --coverage)
            COVERAGE="--cov=app.workers.worker --cov-report=html --cov-report=term"
            shift
            ;;
        -k)
            TEST_FILTER="-k $2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [-v] [--coverage] [-k test_pattern]"
            exit 1
            ;;
    esac
done

echo "Running timeout mechanism tests..."
echo ""

# Run tests
python3 -m pytest tests/test_timeout_mechanism.py $VERBOSE $COVERAGE $TEST_FILTER

echo ""
echo "========================================"
echo "✅ All tests completed"
echo "========================================"

if [ -n "$COVERAGE" ]; then
    echo ""
    echo "Coverage report generated in htmlcov/index.html"
    echo "Open with: open htmlcov/index.html"
fi
