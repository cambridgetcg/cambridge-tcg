#!/bin/bash
set -euo pipefail

# ============================================================================
# Sales Sync — AWS Deployment Script
#
# Creates: sales_events + platform_listings tables, Shopify webhook Lambda,
#          eBay order poller Lambda, API Gateway endpoint, EventBridge schedule,
#          security group (RDS + HTTPS egress).
#
# Prerequisites:
#   - AWS CLI configured (aws sts get-caller-identity works)
#   - NAT Gateway or public RDS endpoint (Lambdas need both VPC/RDS and internet)
#   - Shopify webhook secret configured
#   - eBay credentials in Secrets Manager
#
# Usage: bash stock/sync/orders/deploy.sh
# ============================================================================

REGION="us-east-1"
ACCOUNT_ID="034362054546"

# --- Infrastructure from existing Lambdas ---
VPC_ID="vpc-073cdce8e84cbccdc"
SUBNET_IDS="subnet-01095623139ea0f77,subnet-05f2d8747e37bf970,subnet-0d846dd0951910224,subnet-03b96e0280a920cf0,subnet-0615ce339a1e17101,subnet-04dd86b8074d9055f"
RDS_ONLY_SG="sg-0b224ea9e0b04b7ba"
RDS_PROXY_SG="sg-0c2766ac3105f4f4b"
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/service-role/cardrush_scraper-1755596135935"
LAYER_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:layer:price-scraper-py312:1"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

# --- DB config (from .env or environment) ---
if [ -f "${REPO_ROOT}/.env" ]; then
    source "${REPO_ROOT}/.env"
fi
: "${PROXY_ENDPOINT:?ERROR: PROXY_ENDPOINT not set. Copy .env.example to .env and fill values.}"
: "${DB_USER:?ERROR: DB_USER not set.}"
: "${DB_PASSWORD:?ERROR: DB_PASSWORD not set.}"
DB_NAME="${DATABASE_NAME:-op_cardrush_link}"

# --- Shopify config ---
SHOPIFY_STORE="${SHOPIFY_STORE:-}"
SHOPIFY_CLIENT_ID="${SHOPIFY_CLIENT_ID:-}"
SHOPIFY_CLIENT_SECRET="${SHOPIFY_CLIENT_SECRET:-}"
SHOPIFY_API_VERSION="${SHOPIFY_API_VERSION:-2024-10}"
SHOPIFY_WEBHOOK_SECRET="${SHOPIFY_WEBHOOK_SECRET:-}"

# --- Naming ---
SALES_SYNC_SG_NAME="sales-sync-sg"
WEBHOOK_FUNCTION="shopify-order-webhook"
POLLER_FUNCTION="ebay-order-poller"
API_NAME="sales-sync-api"
MIGRATION_FUNCTION="sales-sync-migration-runner"

echo "============================================================"
echo "Sales Sync — AWS Deployment"
echo "Region: ${REGION}"
echo "Account: ${ACCOUNT_ID}"
echo "============================================================"

# ----------------------------------------------------------------
# Step 0: Validate
# ----------------------------------------------------------------
echo ""
echo "[0/8] Validating AWS credentials..."
aws sts get-caller-identity --output text > /dev/null
echo "  OK"

# ----------------------------------------------------------------
# Step 1: Run database migration
# ----------------------------------------------------------------
echo ""
echo "[1/8] Running database migration (sales_events + platform_listings)..."

MIGRATION_SQL=$(cat "${REPO_ROOT}/pricing/migrations/004_add_sales_events.sql")

MIGRATION_DIR=$(mktemp -d)
cat > "${MIGRATION_DIR}/lambda_function.py" << 'PYEOF'
import os
import psycopg2

def lambda_handler(event, context):
    sql = event.get('sql', '')
    connection = psycopg2.connect(
        host=os.environ['PROXY_ENDPOINT'],
        database=os.environ.get('DATABASE_NAME', 'op_cardrush_link'),
        user=os.environ['DB_USER'],
        password=os.environ['DB_PASSWORD'],
        port=int(os.environ.get('DB_PORT', 5432)),
        connect_timeout=10
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            connection.commit()
        # Verify tables exist
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM sales_events")
            se_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM platform_listings")
            pl_count = cursor.fetchone()[0]
        return {
            'statusCode': 200,
            'body': f'Migration complete. sales_events={se_count}, platform_listings={pl_count}'
        }
    except Exception as e:
        connection.rollback()
        return {'statusCode': 500, 'body': str(e)}
    finally:
        connection.close()
PYEOF

(cd "${MIGRATION_DIR}" && zip -q migration.zip lambda_function.py)

if aws lambda get-function --function-name "${MIGRATION_FUNCTION}" > /dev/null 2>&1; then
    aws lambda update-function-code \
        --function-name "${MIGRATION_FUNCTION}" \
        --zip-file "fileb://${MIGRATION_DIR}/migration.zip" \
        --output text > /dev/null
    echo "  Updated existing migration Lambda"
else
    aws lambda create-function \
        --function-name "${MIGRATION_FUNCTION}" \
        --runtime python3.12 \
        --handler lambda_function.lambda_handler \
        --role "${ROLE_ARN}" \
        --zip-file "fileb://${MIGRATION_DIR}/migration.zip" \
        --timeout 30 \
        --memory-size 128 \
        --architectures arm64 \
        --layers "${LAYER_ARN}" \
        --vpc-config "SubnetIds=${SUBNET_IDS},SecurityGroupIds=${RDS_ONLY_SG}" \
        --environment "Variables={PROXY_ENDPOINT=${PROXY_ENDPOINT},DB_USER=${DB_USER},DB_PASSWORD=${DB_PASSWORD},DATABASE_NAME=${DB_NAME},DB_PORT=5432}" \
        --output text > /dev/null
    echo "  Created migration Lambda"
fi

echo "  Waiting for Lambda to be active..."
aws lambda wait function-active-v2 --function-name "${MIGRATION_FUNCTION}"

echo "  Running migration SQL..."
MIGRATION_PAYLOAD=$(printf '{"sql": %s}' "$(echo "${MIGRATION_SQL}" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))')")

MIGRATION_RESULT=$(aws lambda invoke \
    --function-name "${MIGRATION_FUNCTION}" \
    --payload "${MIGRATION_PAYLOAD}" \
    --cli-binary-format raw-in-base64-out \
    /tmp/sales_migration_result.json \
    --output text --query 'StatusCode' 2>&1)

MIGRATION_BODY=$(cat /tmp/sales_migration_result.json)
echo "  Result: ${MIGRATION_BODY}"

echo "  Cleaning up migration Lambda..."
aws lambda delete-function --function-name "${MIGRATION_FUNCTION}" 2>/dev/null || true
rm -rf "${MIGRATION_DIR}"

# ----------------------------------------------------------------
# Step 2: Create security group (RDS + HTTPS egress)
# ----------------------------------------------------------------
echo ""
echo "[2/8] Creating security group for sales sync Lambdas..."

SALES_SYNC_SG=$(aws ec2 describe-security-groups \
    --filters "Name=group-name,Values=${SALES_SYNC_SG_NAME}" "Name=vpc-id,Values=${VPC_ID}" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null)

if [ "${SALES_SYNC_SG}" = "None" ] || [ -z "${SALES_SYNC_SG}" ]; then
    SALES_SYNC_SG=$(aws ec2 create-security-group \
        --group-name "${SALES_SYNC_SG_NAME}" \
        --description "Sales sync Lambdas: egress to RDS (5432) and HTTPS (443)" \
        --vpc-id "${VPC_ID}" \
        --query 'GroupId' --output text)
    echo "  Created SG: ${SALES_SYNC_SG}"

    # Revoke default allow-all egress
    aws ec2 revoke-security-group-egress \
        --group-id "${SALES_SYNC_SG}" \
        --ip-permissions '[{"IpProtocol":"-1","FromPort":-1,"ToPort":-1,"IpRanges":[{"CidrBlock":"0.0.0.0/0"}]}]' \
        2>/dev/null || true

    # Egress: port 5432 to RDS proxy SG
    aws ec2 authorize-security-group-egress \
        --group-id "${SALES_SYNC_SG}" \
        --ip-permissions "[{\"IpProtocol\":\"tcp\",\"FromPort\":5432,\"ToPort\":5432,\"UserIdGroupPairs\":[{\"GroupId\":\"${RDS_PROXY_SG}\"}]}]" \
        2>/dev/null || true

    # Egress: port 443 to 0.0.0.0/0 (internet via NAT for eBay/Shopify APIs)
    aws ec2 authorize-security-group-egress \
        --group-id "${SALES_SYNC_SG}" \
        --ip-permissions '[{"IpProtocol":"tcp","FromPort":443,"ToPort":443,"IpRanges":[{"CidrBlock":"0.0.0.0/0"}]}]' \
        2>/dev/null || true

    # Allow sales-sync SG to reach RDS proxy
    aws ec2 authorize-security-group-ingress \
        --group-id "${RDS_PROXY_SG}" \
        --ip-permissions "[{\"IpProtocol\":\"tcp\",\"FromPort\":5432,\"ToPort\":5432,\"UserIdGroupPairs\":[{\"GroupId\":\"${SALES_SYNC_SG}\"}]}]" \
        2>/dev/null || true
    echo "  Configured SG rules (RDS 5432 + HTTPS 443)"
else
    echo "  SG already exists: ${SALES_SYNC_SG}"
fi

# ----------------------------------------------------------------
# Step 3: Package Lambda code
# ----------------------------------------------------------------
echo ""
echo "[3/8] Packaging Lambda code..."

DEPLOY_DIR=$(mktemp -d)

# Copy required source files preserving directory structure
mkdir -p "${DEPLOY_DIR}/stock/sync/orders"
mkdir -p "${DEPLOY_DIR}/stock/sync/ebay"
mkdir -p "${DEPLOY_DIR}/stock/sync/shopify"
mkdir -p "${DEPLOY_DIR}/stock/count"
mkdir -p "${DEPLOY_DIR}/pricing/push/ebay"

# Core sync files
cp "${REPO_ROOT}/stock/sync/orders/cross_sync.py" "${DEPLOY_DIR}/stock/sync/orders/"
cp "${REPO_ROOT}/stock/sync/orders/shopify_webhook.py" "${DEPLOY_DIR}/stock/sync/orders/"
cp "${REPO_ROOT}/stock/sync/orders/ebay_poller.py" "${DEPLOY_DIR}/stock/sync/orders/"
cp "${REPO_ROOT}/stock/sync/orders/__init__.py" "${DEPLOY_DIR}/stock/sync/orders/"

# eBay client + auth
cp "${REPO_ROOT}/stock/sync/ebay/client.py" "${DEPLOY_DIR}/stock/sync/ebay/"
cp "${REPO_ROOT}/stock/sync/ebay/__init__.py" "${DEPLOY_DIR}/stock/sync/ebay/"

# Shopify client + auth
cp "${REPO_ROOT}/stock/sync/shopify/client.py" "${DEPLOY_DIR}/stock/sync/shopify/"
cp "${REPO_ROOT}/stock/sync/shopify/auth.py" "${DEPLOY_DIR}/stock/sync/shopify/"
cp "${REPO_ROOT}/stock/sync/shopify/__init__.py" "${DEPLOY_DIR}/stock/sync/shopify/"

# Stock __init__ files for package resolution
cp "${REPO_ROOT}/stock/__init__.py" "${DEPLOY_DIR}/stock/"
cp "${REPO_ROOT}/stock/sync/__init__.py" "${DEPLOY_DIR}/stock/sync/"
cp "${REPO_ROOT}/stock/count/__init__.py" "${DEPLOY_DIR}/stock/count/"

# eBay auth (pricing/push/ebay/ebay_auth.py — required by ebay client)
cp "${REPO_ROOT}/pricing/push/ebay/ebay_auth.py" "${DEPLOY_DIR}/pricing/push/ebay/"
touch "${DEPLOY_DIR}/pricing/__init__.py"
touch "${DEPLOY_DIR}/pricing/push/__init__.py"
touch "${DEPLOY_DIR}/pricing/push/ebay/__init__.py"

# Create webhook handler entry point (API Gateway expects handler at top level)
cat > "${DEPLOY_DIR}/webhook_handler.py" << 'PYEOF'
from stock.sync.orders.shopify_webhook import lambda_handler
PYEOF

# Create poller handler entry point
cat > "${DEPLOY_DIR}/poller_handler.py" << 'PYEOF'
from stock.sync.orders.ebay_poller import lambda_handler
PYEOF

# Package
(cd "${DEPLOY_DIR}" && zip -rq sales-sync.zip . -x "*.pyc" "__pycache__/*")
echo "  Package size: $(du -h "${DEPLOY_DIR}/sales-sync.zip" | cut -f1)"

# ----------------------------------------------------------------
# Step 4: Deploy Shopify webhook Lambda
# ----------------------------------------------------------------
echo ""
echo "[4/8] Deploying Shopify webhook Lambda..."

WEBHOOK_ENV="Variables={PROXY_ENDPOINT=${PROXY_ENDPOINT},DB_USER=${DB_USER},DB_PASSWORD=${DB_PASSWORD},DATABASE_NAME=${DB_NAME},DB_PORT=5432"
if [ -n "${SHOPIFY_WEBHOOK_SECRET}" ]; then
    WEBHOOK_ENV="${WEBHOOK_ENV},SHOPIFY_WEBHOOK_SECRET=${SHOPIFY_WEBHOOK_SECRET}"
fi
WEBHOOK_ENV="${WEBHOOK_ENV}}"

if aws lambda get-function --function-name "${WEBHOOK_FUNCTION}" > /dev/null 2>&1; then
    aws lambda update-function-code \
        --function-name "${WEBHOOK_FUNCTION}" \
        --zip-file "fileb://${DEPLOY_DIR}/sales-sync.zip" \
        --output text > /dev/null
    echo "  Updated existing webhook Lambda code"

    aws lambda wait function-updated-v2 --function-name "${WEBHOOK_FUNCTION}"

    aws lambda update-function-configuration \
        --function-name "${WEBHOOK_FUNCTION}" \
        --handler webhook_handler.lambda_handler \
        --timeout 30 \
        --memory-size 256 \
        --layers "${LAYER_ARN}" \
        --vpc-config "SubnetIds=${SUBNET_IDS},SecurityGroupIds=${SALES_SYNC_SG}" \
        --environment "${WEBHOOK_ENV}" \
        --output text > /dev/null
    echo "  Updated webhook Lambda config"
else
    aws lambda create-function \
        --function-name "${WEBHOOK_FUNCTION}" \
        --runtime python3.12 \
        --handler webhook_handler.lambda_handler \
        --role "${ROLE_ARN}" \
        --zip-file "fileb://${DEPLOY_DIR}/sales-sync.zip" \
        --timeout 30 \
        --memory-size 256 \
        --architectures arm64 \
        --layers "${LAYER_ARN}" \
        --vpc-config "SubnetIds=${SUBNET_IDS},SecurityGroupIds=${SALES_SYNC_SG}" \
        --environment "${WEBHOOK_ENV}" \
        --output text > /dev/null
    echo "  Created webhook Lambda"
fi

aws lambda wait function-active-v2 --function-name "${WEBHOOK_FUNCTION}"
echo "  Lambda is active"

# ----------------------------------------------------------------
# Step 5: Deploy eBay poller Lambda
# ----------------------------------------------------------------
echo ""
echo "[5/8] Deploying eBay order poller Lambda..."

POLLER_ENV="Variables={PROXY_ENDPOINT=${PROXY_ENDPOINT},DB_USER=${DB_USER},DB_PASSWORD=${DB_PASSWORD},DATABASE_NAME=${DB_NAME},DB_PORT=5432"
if [ -n "${SHOPIFY_STORE}" ]; then
    POLLER_ENV="${POLLER_ENV},SHOPIFY_STORE=${SHOPIFY_STORE},SHOPIFY_CLIENT_ID=${SHOPIFY_CLIENT_ID},SHOPIFY_CLIENT_SECRET=${SHOPIFY_CLIENT_SECRET},SHOPIFY_API_VERSION=${SHOPIFY_API_VERSION}"
fi
POLLER_ENV="${POLLER_ENV}}"

if aws lambda get-function --function-name "${POLLER_FUNCTION}" > /dev/null 2>&1; then
    aws lambda update-function-code \
        --function-name "${POLLER_FUNCTION}" \
        --zip-file "fileb://${DEPLOY_DIR}/sales-sync.zip" \
        --output text > /dev/null
    echo "  Updated existing poller Lambda code"

    aws lambda wait function-updated-v2 --function-name "${POLLER_FUNCTION}"

    aws lambda update-function-configuration \
        --function-name "${POLLER_FUNCTION}" \
        --handler poller_handler.lambda_handler \
        --timeout 120 \
        --memory-size 256 \
        --layers "${LAYER_ARN}" \
        --vpc-config "SubnetIds=${SUBNET_IDS},SecurityGroupIds=${SALES_SYNC_SG}" \
        --environment "${POLLER_ENV}" \
        --output text > /dev/null
    echo "  Updated poller Lambda config"
else
    aws lambda create-function \
        --function-name "${POLLER_FUNCTION}" \
        --runtime python3.12 \
        --handler poller_handler.lambda_handler \
        --role "${ROLE_ARN}" \
        --zip-file "fileb://${DEPLOY_DIR}/sales-sync.zip" \
        --timeout 120 \
        --memory-size 256 \
        --architectures arm64 \
        --layers "${LAYER_ARN}" \
        --vpc-config "SubnetIds=${SUBNET_IDS},SecurityGroupIds=${SALES_SYNC_SG}" \
        --environment "${POLLER_ENV}" \
        --output text > /dev/null
    echo "  Created poller Lambda"
fi

aws lambda wait function-active-v2 --function-name "${POLLER_FUNCTION}"
echo "  Lambda is active"

# ----------------------------------------------------------------
# Step 6: Create API Gateway HTTP API for Shopify webhook
# ----------------------------------------------------------------
echo ""
echo "[6/8] Creating API Gateway for Shopify webhook..."

API_ID=$(aws apigatewayv2 get-apis \
    --query "Items[?Name=='${API_NAME}'].ApiId | [0]" \
    --output text 2>/dev/null)

if [ "${API_ID}" = "None" ] || [ -z "${API_ID}" ]; then
    API_ID=$(aws apigatewayv2 create-api \
        --name "${API_NAME}" \
        --protocol-type HTTP \
        --query 'ApiId' --output text)
    echo "  Created API: ${API_ID}"
else
    echo "  API already exists: ${API_ID}"
fi

# Create Lambda integration
INTEGRATION_ID=$(aws apigatewayv2 get-integrations \
    --api-id "${API_ID}" \
    --query "Items[0].IntegrationId" \
    --output text 2>/dev/null)

WEBHOOK_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${WEBHOOK_FUNCTION}"

if [ "${INTEGRATION_ID}" = "None" ] || [ -z "${INTEGRATION_ID}" ]; then
    INTEGRATION_ID=$(aws apigatewayv2 create-integration \
        --api-id "${API_ID}" \
        --integration-type AWS_PROXY \
        --integration-uri "${WEBHOOK_ARN}" \
        --payload-format-version "2.0" \
        --query 'IntegrationId' --output text)
    echo "  Created integration: ${INTEGRATION_ID}"
fi

# Create route: POST /webhooks/shopify/orders
ROUTE_KEY="POST /webhooks/shopify/orders"
ROUTE_ID=$(aws apigatewayv2 get-routes \
    --api-id "${API_ID}" \
    --query "Items[?RouteKey=='${ROUTE_KEY}'].RouteId | [0]" \
    --output text 2>/dev/null)

if [ "${ROUTE_ID}" = "None" ] || [ -z "${ROUTE_ID}" ]; then
    aws apigatewayv2 create-route \
        --api-id "${API_ID}" \
        --route-key "${ROUTE_KEY}" \
        --target "integrations/${INTEGRATION_ID}" \
        --output text > /dev/null
    echo "  Created route: ${ROUTE_KEY}"
fi

# Create default stage with auto-deploy
STAGE_NAME_VAL="\$default"
aws apigatewayv2 create-stage \
    --api-id "${API_ID}" \
    --stage-name "${STAGE_NAME_VAL}" \
    --auto-deploy \
    --output text > /dev/null 2>&1 || true

# Grant API Gateway permission to invoke Lambda
aws lambda add-permission \
    --function-name "${WEBHOOK_FUNCTION}" \
    --statement-id "apigateway-invoke" \
    --action "lambda:InvokeFunction" \
    --principal "apigateway.amazonaws.com" \
    --source-arn "arn:aws:execute-api:${REGION}:${ACCOUNT_ID}:${API_ID}/*/*" \
    2>/dev/null || true

WEBHOOK_URL="https://${API_ID}.execute-api.${REGION}.amazonaws.com/webhooks/shopify/orders"
echo "  Webhook URL: ${WEBHOOK_URL}"

# ----------------------------------------------------------------
# Step 7: Create EventBridge schedule for eBay poller (DISABLED)
# ----------------------------------------------------------------
echo ""
echo "[7/8] Creating EventBridge schedule for eBay poller..."

POLLER_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${POLLER_FUNCTION}"
RULE_NAME="ebay-order-poller-schedule"

# Create or update rule (starts DISABLED for safety)
aws events put-rule \
    --name "${RULE_NAME}" \
    --schedule-expression "rate(5 minutes)" \
    --state DISABLED \
    --description "Poll eBay for new orders every 5 minutes" \
    --output text > /dev/null

aws events put-targets \
    --rule "${RULE_NAME}" \
    --targets "Id=ebay-order-poller,Arn=${POLLER_ARN}" \
    --output text > /dev/null

# Grant EventBridge permission to invoke Lambda
aws lambda add-permission \
    --function-name "${POLLER_FUNCTION}" \
    --statement-id "eventbridge-invoke" \
    --action "lambda:InvokeFunction" \
    --principal "events.amazonaws.com" \
    --source-arn "arn:aws:events:${REGION}:${ACCOUNT_ID}:rule/${RULE_NAME}" \
    2>/dev/null || true

echo "  Rule created: ${RULE_NAME} (DISABLED — enable when ready)"

# ----------------------------------------------------------------
# Step 8: Register Shopify webhooks
# ----------------------------------------------------------------
echo ""
echo "[8/9] Registering Shopify webhooks..."

if [ -n "${SHOPIFY_STORE}" ] && [ -n "${SHOPIFY_CLIENT_ID}" ] && [ -n "${SHOPIFY_CLIENT_SECRET}" ]; then
    REGISTER_DIR=$(mktemp -d)

    # Copy Shopify client + auth for webhook registration
    mkdir -p "${REGISTER_DIR}/stock/sync/shopify"
    cp "${REPO_ROOT}/stock/sync/shopify/client.py" "${REGISTER_DIR}/stock/sync/shopify/"
    cp "${REPO_ROOT}/stock/sync/shopify/auth.py" "${REGISTER_DIR}/stock/sync/shopify/"
    cp "${REPO_ROOT}/stock/sync/shopify/__init__.py" "${REGISTER_DIR}/stock/sync/shopify/"
    touch "${REGISTER_DIR}/stock/__init__.py"
    touch "${REGISTER_DIR}/stock/sync/__init__.py"

    cat > "${REGISTER_DIR}/lambda_function.py" << 'PYEOF'
import json
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))
from stock.sync.shopify.client import ShopifyClient

def lambda_handler(event, context):
    callback_url = event['callback_url']
    try:
        client = ShopifyClient()
        result = client.ensure_webhooks(callback_url)
        return {
            'statusCode': 200,
            'body': json.dumps(result),
        }
    except Exception as e:
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)[:500]}),
        }
PYEOF

    (cd "${REGISTER_DIR}" && pip3 install -q requests -t . 2>/dev/null && zip -rq register.zip . -x "*.pyc" "__pycache__/*")

    REGISTER_FUNCTION="shopify-webhook-register"

    # This Lambda does NOT need VPC (only talks to Shopify HTTPS)
    if aws lambda get-function --function-name "${REGISTER_FUNCTION}" > /dev/null 2>&1; then
        aws lambda update-function-code \
            --function-name "${REGISTER_FUNCTION}" \
            --zip-file "fileb://${REGISTER_DIR}/register.zip" \
            --output text > /dev/null
        echo "  Updated register Lambda"
    else
        aws lambda create-function \
            --function-name "${REGISTER_FUNCTION}" \
            --runtime python3.12 \
            --handler lambda_function.lambda_handler \
            --role "${ROLE_ARN}" \
            --zip-file "fileb://${REGISTER_DIR}/register.zip" \
            --timeout 30 \
            --memory-size 128 \
            --architectures arm64 \
            --environment "Variables={SHOPIFY_STORE=${SHOPIFY_STORE},SHOPIFY_CLIENT_ID=${SHOPIFY_CLIENT_ID},SHOPIFY_CLIENT_SECRET=${SHOPIFY_CLIENT_SECRET},SHOPIFY_API_VERSION=${SHOPIFY_API_VERSION}}" \
            --output text > /dev/null
        echo "  Created register Lambda"
    fi

    echo "  Waiting for Lambda to be active..."
    aws lambda wait function-active-v2 --function-name "${REGISTER_FUNCTION}"

    echo "  Registering webhooks for: ${WEBHOOK_URL}"
    REGISTER_PAYLOAD=$(printf '{"callback_url": "%s"}' "${WEBHOOK_URL}")

    aws lambda invoke \
        --function-name "${REGISTER_FUNCTION}" \
        --payload "${REGISTER_PAYLOAD}" \
        --cli-binary-format raw-in-base64-out \
        /tmp/webhook_register_result.json \
        --output text --query 'StatusCode' > /dev/null 2>&1

    REGISTER_BODY=$(cat /tmp/webhook_register_result.json)
    echo "  Result: ${REGISTER_BODY}"

    echo "  Cleaning up register Lambda..."
    aws lambda delete-function --function-name "${REGISTER_FUNCTION}" 2>/dev/null || true
    rm -rf "${REGISTER_DIR}"
else
    echo "  SKIPPED: SHOPIFY_STORE, SHOPIFY_CLIENT_ID, or SHOPIFY_CLIENT_SECRET not set"
    echo "  Register manually after setting Shopify env vars:"
    echo "    python3 -c \"from stock.sync.shopify.client import ShopifyClient; print(ShopifyClient().ensure_webhooks('${WEBHOOK_URL}'))\""
fi

# ----------------------------------------------------------------
# Step 9: Summary
# ----------------------------------------------------------------
echo ""
echo "[9/9] Cleanup..."
rm -rf "${DEPLOY_DIR}"

echo ""
echo "============================================================"
echo "Sales Sync — Deployment Complete"
echo "============================================================"
echo ""
echo "Lambdas:"
echo "  ${WEBHOOK_FUNCTION}: Shopify order webhook handler"
echo "  ${POLLER_FUNCTION}: eBay order poller (5-min schedule)"
echo ""
echo "API Gateway:"
echo "  ${WEBHOOK_URL}"
echo ""
echo "Security Group: ${SALES_SYNC_SG}"
echo ""
echo "NEXT STEPS:"
echo "  1. Configure SHOPIFY_WEBHOOK_SECRET in webhook Lambda env"
echo "  2. Configure Shopify/eBay credentials in poller Lambda env"
echo "  3. Run push scripts to populate platform_listings cache:"
echo "     python -m stock.count.push_ebay_stock --dry-run"
echo "     python -m stock.count.push_shopify_stock --dry-run"
echo "  4. Enable eBay poller schedule:"
echo "     aws events enable-rule --name ${RULE_NAME}"
echo "  5. IMPORTANT: Ensure NAT Gateway is configured for Lambda VPC subnets"
echo "     (Lambdas need internet access for eBay/Shopify API calls)"
echo "============================================================"
