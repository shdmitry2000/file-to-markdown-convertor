#!/bin/bash
# Master test runner - runs all tests in order
# Usage: ./run_all_tests.sh [unit|standalone|docker|all]

set -e

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

MODE="${1:-all}"

echo ""
echo "╔════════════════════════════════════════════╗"
echo "║   Markdown Converter Test Suite           ║"
echo "╚════════════════════════════════════════════╝"
echo ""

run_unit_tests() {
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}📋 Running Unit Tests (pytest)${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    
    if pytest tests/ -v --tb=short; then
        echo ""
        echo -e "${GREEN}✅ Unit tests passed${NC}"
        return 0
    else
        echo ""
        echo -e "${RED}❌ Unit tests failed${NC}"
        return 1
    fi
}

run_standalone_tests() {
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}🖥️  Running Standalone Integration Tests${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    
    if ./test_standalone.sh; then
        echo ""
        echo -e "${GREEN}✅ Standalone tests passed${NC}"
        return 0
    else
        echo ""
        echo -e "${RED}❌ Standalone tests failed${NC}"
        return 1
    fi
}

run_docker_tests() {
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}🐳 Running Docker Integration Tests${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    
    if ./test_docker.sh; then
        echo ""
        echo -e "${GREEN}✅ Docker tests passed${NC}"
        return 0
    else
        echo ""
        echo -e "${RED}❌ Docker tests failed${NC}"
        return 1
    fi
}

# Track results
UNIT_RESULT=0
STANDALONE_RESULT=0
DOCKER_RESULT=0

# Run selected tests
case "$MODE" in
    unit)
        run_unit_tests || UNIT_RESULT=$?
        ;;
    standalone)
        run_standalone_tests || STANDALONE_RESULT=$?
        ;;
    docker)
        run_docker_tests || DOCKER_RESULT=$?
        ;;
    all)
        run_unit_tests || UNIT_RESULT=$?
        run_standalone_tests || STANDALONE_RESULT=$?
        run_docker_tests || DOCKER_RESULT=$?
        ;;
    *)
        echo -e "${RED}Invalid mode: $MODE${NC}"
        echo "Usage: $0 [unit|standalone|docker|all]"
        exit 1
        ;;
esac

# Print summary
echo ""
echo "╔════════════════════════════════════════════╗"
echo "║   Test Summary                             ║"
echo "╚════════════════════════════════════════════╝"
echo ""

if [ "$MODE" = "all" ] || [ "$MODE" = "unit" ]; then
    if [ $UNIT_RESULT -eq 0 ]; then
        echo -e "   Unit Tests:       ${GREEN}✅ PASSED${NC}"
    else
        echo -e "   Unit Tests:       ${RED}❌ FAILED${NC}"
    fi
fi

if [ "$MODE" = "all" ] || [ "$MODE" = "standalone" ]; then
    if [ $STANDALONE_RESULT -eq 0 ]; then
        echo -e "   Standalone Tests: ${GREEN}✅ PASSED${NC}"
    else
        echo -e "   Standalone Tests: ${RED}❌ FAILED${NC}"
    fi
fi

if [ "$MODE" = "all" ] || [ "$MODE" = "docker" ]; then
    if [ $DOCKER_RESULT -eq 0 ]; then
        echo -e "   Docker Tests:     ${GREEN}✅ PASSED${NC}"
    else
        echo -e "   Docker Tests:     ${RED}❌ FAILED${NC}"
    fi
fi

echo ""

# Exit with failure if any tests failed
if [ $UNIT_RESULT -ne 0 ] || [ $STANDALONE_RESULT -ne 0 ] || [ $DOCKER_RESULT -ne 0 ]; then
    echo -e "${RED}❌ Some tests failed${NC}"
    exit 1
else
    echo -e "${GREEN}✅ All tests passed!${NC}"
    exit 0
fi
