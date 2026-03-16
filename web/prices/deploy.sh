#!/bin/bash
set -euo pipefail

# ============================================================================
# Price Explorer — Cloudflare Pages Deploy
#
# Deploys web/prices/ as a Cloudflare Pages project.
# Free hosting, CDN, SSL — no S3/CloudFront needed.
#
# Prerequisites:
#   - wrangler CLI authenticated (wrangler login)
#   - cambridgetcg.com zone active on Cloudflare
#
# First deploy:  bash web/prices/deploy.sh --setup
# Subsequent:    bash web/prices/deploy.sh
# ============================================================================

PROJECT_NAME="price-explorer"
CUSTOM_DOMAIN="prices.cambridgetcg.com"
ACCOUNT_ID="cf4198e651bf3009877d49f688c9d88e"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "============================================================"
echo "Price Explorer — Cloudflare Pages Deploy"
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
echo "[2/4] Updating cache-busting version..."
CACHE_VER="v=$(date +%Y%m%d%H%M%S)"
# Replace ?v=... query params in index.html with fresh timestamp
sed -i '' -E "s/\?v=[0-9a-zA-Z]+/?${CACHE_VER}/g" "${SCRIPT_DIR}/index.html"
echo "  Cache version: ${CACHE_VER}"

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

# Check if custom domain is configured
if [ "${1:-}" = "--setup" ]; then
    echo ""
    echo "  To add custom domain (one-time, via dashboard):"
    echo "    1. Go to: https://dash.cloudflare.com → Pages → ${PROJECT_NAME} → Custom domains"
    echo "    2. Click 'Set up a custom domain'"
    echo "    3. Enter: ${CUSTOM_DOMAIN}"
    echo "    4. Cloudflare auto-creates the CNAME record"
    echo ""
    echo "  Or via API (requires dns_records:edit token):"
    echo "    ZONE_ID=\$(curl -s 'https://api.cloudflare.com/client/v4/zones?name=cambridgetcg.com' -H 'Authorization: Bearer TOKEN' | jq -r '.result[0].id')"
    echo "    curl -X POST 'https://api.cloudflare.com/client/v4/zones/\$ZONE_ID/dns_records' \\"
    echo "      -H 'Authorization: Bearer TOKEN' \\"
    echo "      -d '{\"type\":\"CNAME\",\"name\":\"prices\",\"content\":\"${PROJECT_NAME}.pages.dev\",\"proxied\":true}'"
fi

echo ""
echo "============================================================"
echo "Deploy complete!"
echo ""
echo "Live at:"
echo "  https://${PROJECT_NAME}.pages.dev (immediate)"
echo "  https://${CUSTOM_DOMAIN} (after custom domain setup)"
echo "============================================================"
