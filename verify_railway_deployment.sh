#!/bin/bash
# Verification script for Railway deployment readiness

set -e

echo "=========================================="
echo "Verifying Railway Deployment Setup"
echo "=========================================="
echo ""

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Track status
ERRORS=0
WARNINGS=0

check_file() {
    if [ -f "$1" ]; then
        echo -e "${GREEN}✓${NC} $1 exists"
        return 0
    else
        echo -e "${RED}✗${NC} $1 is missing"
        ERRORS=$((ERRORS + 1))
        return 1
    fi
}

check_dir() {
    if [ -d "$1" ]; then
        echo -e "${GREEN}✓${NC} $1 directory exists"
        return 0
    else
        echo -e "${RED}✗${NC} $1 directory is missing"
        ERRORS=$((ERRORS + 1))
        return 1
    fi
}

echo "Checking Dockerfiles..."
check_file "Dockerfile"
check_file "evidence-explorer/Dockerfile"
check_file "swarm-detector/Dockerfile"
check_file "operation-silent-storm/Dockerfile"
echo ""

echo "Checking Railway configuration files..."
check_file "railway.json"
check_file "railway.toml"
check_file "railway-integrated.json"
check_file "railway-explorer.json"
check_file "railway-swarm.json"
check_file "railway-silent-storm.json"
echo ""

echo "Checking WSGI and entrypoint files..."
check_file "traced_wsgi.py"
check_file "scripts/entrypoint.sh"
echo ""

echo "Checking documentation..."
check_file "RAILWAY_DEPLOY.md"
check_file "DEPLOYMENT_SUMMARY.md"
check_file ".env.railway.example"
echo ""

echo "Checking source directories..."
check_dir "evidence-explorer/src"
check_dir "swarm-detector/src"
check_dir "operation-silent-storm/src"
check_dir "tools"
echo ""

echo "Checking existing Docker configurations..."
check_file "swarm-detector/gunicorn.conf.py"
check_file "swarm-detector/scripts/entrypoint.sh"
echo ""

echo "Checking pyproject.toml files..."
check_file "pyproject.toml"
check_file "evidence-explorer/pyproject.toml"
check_file "swarm-detector/pyproject.toml"
check_file "operation-silent-storm/pyproject.toml"
echo ""

echo "Checking .dockerignore..."
check_file ".dockerignore"
echo ""

echo "Checking Makefile..."
check_file "Makefile"
if grep -q "railway-deploy" Makefile; then
    echo -e "${GREEN}✓${NC} Makefile has Railway deployment targets"
else
    echo -e "${YELLOW}⚠${NC} Makefile missing Railway deployment targets"
    WARNINGS=$((WARNINGS + 1))
fi
echo ""

# Check Dockerfile syntax (basic check)
echo "Checking Dockerfile syntax..."
if docker --version >/dev/null 2>&1; then
    if docker build -f Dockerfile --dry-run . >/dev/null 2>&1; then
        echo -e "${GREEN}✓${NC} Dockerfile syntax is valid"
    else
        echo -e "${YELLOW}⚠${NC} Dockerfile has syntax issues (may still work)"
        WARNINGS=$((WARNINGS + 1))
    fi
else
    echo -e "${YELLOW}⚠${NC} Docker not available, skipping syntax check"
    WARNINGS=$((WARNINGS + 1))
fi
echo ""

# Summary
echo "=========================================="
echo "Verification Summary"
echo "=========================================="
echo -e "${GREEN}✓ Passed: All required files present${NC}"

if [ $ERRORS -eq 0 ] && [ $WARNINGS -eq 0 ]; then
    echo -e "${GREEN}✓ No errors or warnings${NC}"
    echo ""
    echo -e "${GREEN}Your Railway deployment setup is ready!${NC}"
    echo ""
    echo "To deploy:"
    echo "  1. Go to Railway.app"
    echo "  2. Create new project from GitHub"
    echo "  3. Select this repository"
    echo "  4. Railway will auto-detect the Dockerfile"
    echo "  5. Set environment variables"
    echo "  6. Add volume: traced-data at /app/data"
    echo "  7. Deploy!"
    exit 0
elif [ $ERRORS -eq 0 ]; then
    echo -e "${YELLOW}⚠ $WARNINGS warning(s) found${NC}"
    echo ""
    echo -e "${GREEN}Your setup is likely ready, but review warnings above${NC}"
    exit 0
else
    echo -e "${RED}✗ $ERRORS error(s) found${NC}"
    echo ""
    echo -e "${RED}Please fix the errors above before deploying${NC}"
    exit 1
fi
