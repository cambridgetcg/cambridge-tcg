#!/bin/bash
set -euo pipefail

# ============================================================================
# Trade-In Buy List — Cloudflare Pages Deploy
#
# Deploys web/tradein/ as a Cloudflare Pages project.
# Free hosting, CDN, SSL — no S3/CloudFront needed.
#
# Prerequisites:
#   - wrangler CLI authenticated (wrangler login)
#   - cambridgetcg.com zone active on Cloudflare
#
# First deploy:  bash web/tradein/deploy.sh --setup
# Subsequent:    bash web/tradein/deploy.sh
# ============================================================================

PROJECT_NAME="tradein-buylist"
CUSTOM_DOMAIN="tradein.cambridgetcg.com"
ACCOUNT_ID="cf4198e651bf3009877d49f688c9d88e"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "============================================================"
echo "Trade-In Buy List — Cloudflare Pages Deploy"
echo "Project: ${PROJECT_NAME}"
echo "Domain:  ${CUSTOM_DOMAIN}"
echo "============================================================"

# ----------------------------------------------------------------
# Step 0: Validate wrangler auth
# ----------------------------------------------------------------
echo ""
echo "[1/3] Validating wrangler authentication..."
wrangler whoami > /dev/null 2>&1 || { echo "ERROR: Run 'wrangler login' first"; exit 1; }
echo "  Authenticated"

# ----------------------------------------------------------------
# Step 1: Create project (first time only, --setup flag)
# ----------------------------------------------------------------
if [ "${1:-}" = "--setup" ]; then
    echo ""
    echo "[Setup] Creating Pages project..."
    wrangler pages project create "${PROJECT_NAME}" --production-branch main 2>/dev/null || echo "  Project already exists"
    echo "  Project: ${PROJECT_NAME}"
fi

# ----------------------------------------------------------------
# Step 2: Cache-bust static assets
# ----------------------------------------------------------------
echo ""
echo "[2/4] Stamping cache-busting hash..."
HASH=$(cat "${SCRIPT_DIR}"/js/*.js "${SCRIPT_DIR}"/css/*.css | md5sum | cut -c1-8 2>/dev/null \
    || cat "${SCRIPT_DIR}"/js/*.js "${SCRIPT_DIR}"/css/*.css | md5 -q | cut -c1-8)
sed -i.bak -E "s/\?v=[a-f0-9]{8}/?v=${HASH}/g" "${SCRIPT_DIR}/index.html"
rm -f "${SCRIPT_DIR}/index.html.bak"
echo "  Hash: ${HASH}"

# ----------------------------------------------------------------
# Step 3: Deploy files
# ----------------------------------------------------------------
echo ""
echo "[3/4] Deploying to Cloudflare Pages..."
wrangler pages deploy "${SCRIPT_DIR}" \
    --project-name "${PROJECT_NAME}" \
    --branch main \
    --commit-dirty=true

echo "  Deploy complete"

# ----------------------------------------------------------------
# Step 4: Custom domain reminder
# ----------------------------------------------------------------
echo ""
echo "[4/4] Custom domain"

if [ "${1:-}" = "--setup" ]; then
    echo ""
    echo "  To add custom domain (one-time, via dashboard):"
    echo "    1. Go to: https://dash.cloudflare.com → Pages → ${PROJECT_NAME} → Custom domains"
    echo "    2. Click 'Set up a custom domain'"
    echo "    3. Enter: ${CUSTOM_DOMAIN}"
    echo "    4. Cloudflare auto-creates the CNAME record"
fi

echo ""
echo "============================================================"
echo "Deploy complete!"
echo ""
echo "Live at:"
echo "  https://${PROJECT_NAME}.pages.dev (immediate)"
echo "  https://${CUSTOM_DOMAIN} (after custom domain setup)"
echo "============================================================"
