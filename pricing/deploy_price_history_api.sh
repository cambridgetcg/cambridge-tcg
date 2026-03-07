#!/bin/bash
set -euo pipefail

# ============================================================================
# Price History API Lambda — AWS Deployment Script
#
# Creates: IAM role, Lambda function in public VPC subnets, Function URL.
# Public read-only API — no auth (price data is not sensitive).
#
# Prerequisites:
#   - AWS CLI configured
#   - .env file with DB credentials
#
# Usage: bash pricing/deploy_price_history_api.sh
# ============================================================================

REGION="us-east-1"
ACCOUNT_ID="034362054546"

# --- Infrastructure ---
# Public subnets (only needs RDS Proxy, no internet)
PUBLIC_SUBNET_IDS="subnet-01095623139ea0f77,subnet-05f2d8747e37bf970,subnet-0d846dd0951910224,subnet-03b96e0280a920cf0,subnet-0615ce339a1e17101,subnet-04dd86b8074d9055f"
# Health-check SG (has RDS Proxy egress on 5432)
SECURITY_GROUP_IDS="sg-00866258c72d6b39d"
LAYER_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:layer:price-scraper-py312:1"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# --- DB config (from .env or environment) ---
if [ -f "${REPO_ROOT}/.env" ]; then
    source "${REPO_ROOT}/.env"
fi
: "${PROXY_ENDPOINT:?ERROR: PROXY_ENDPOINT not set. Copy .env.example to .env and fill values.}"
: "${DB_USER:?ERROR: DB_USER not set.}"
: "${DB_PASSWORD:?ERROR: DB_PASSWORD not set.}"
: "${ADMIN_SECRET:?ERROR: ADMIN_SECRET not set. Generate with: openssl rand -hex 32}"
DB_NAME="${DATABASE_NAME:-op_cardrush_link}"
SES_ENABLED="${SES_ENABLED:-false}"
SES_SANDBOX_MODE="${SES_SANDBOX_MODE:-true}"

# --- Naming ---
FUNCTION_NAME="price-history-api"
ROLE_NAME="price-history-api-role"

echo "============================================================"
echo "Price History API Lambda — AWS Deployment"
echo "Region: ${REGION}"
echo "Account: ${ACCOUNT_ID}"
echo "============================================================"

# ----------------------------------------------------------------
# Step 0: Validate
# ----------------------------------------------------------------
echo ""
echo "[0/5] Validating AWS credentials..."
aws sts get-caller-identity --output text > /dev/null
echo "  OK"

# ----------------------------------------------------------------
# Step 1: Create IAM role
# ----------------------------------------------------------------
echo ""
echo "[1/5] Creating IAM role..."

TRUST_POLICY='{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "lambda.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}'

ROLE_ARN_FULL="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"

if aws iam get-role --role-name "${ROLE_NAME}" > /dev/null 2>&1; then
    echo "  Role already exists: ${ROLE_NAME}"
    ROLE_ARN_FULL=$(aws iam get-role --role-name "${ROLE_NAME}" --query 'Role.Arn' --output text)
else
    ROLE_ARN_FULL=$(aws iam create-role \
        --role-name "${ROLE_NAME}" \
        --assume-role-policy-document "${TRUST_POLICY}" \
        --description "Price history API Lambda role (read-only)" \
        --query 'Role.Arn' --output text)
    echo "  Created role: ${ROLE_ARN_FULL}"
    echo "  Waiting for role propagation..."
    sleep 15
fi

# Attach VPC access policy
aws iam attach-role-policy \
    --role-name "${ROLE_NAME}" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole \
    2>/dev/null || true
echo "  VPC access policy attached"

# Inline policy: CloudWatch Logs
INLINE_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:${REGION}:${ACCOUNT_ID}:*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "sns:Publish"
      ],
      "Resource": "arn:aws:sns:${REGION}:${ACCOUNT_ID}:cambridge-tcg-pipeline-alerts"
    },
    {
      "Effect": "Allow",
      "Action": [
        "ses:SendEmail",
        "ses:SendRawEmail"
      ],
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "ses:FromAddress": "no-reply@cambridgetcg.com"
        }
      }
    }
  ]
}
EOF
)

aws iam put-role-policy \
    --role-name "${ROLE_NAME}" \
    --policy-name "price-history-api-permissions" \
    --policy-document "${INLINE_POLICY}"
echo "  Inline policy attached (CloudWatch Logs + SNS + SES)"

# ----------------------------------------------------------------
# Step 2: Package Lambda
# ----------------------------------------------------------------
echo ""
echo "[2/5] Packaging Lambda..."

DEPLOY_DIR=$(mktemp -d)
cp "${SCRIPT_DIR}/api/lambda_function.py" "${DEPLOY_DIR}/"
cp "${SCRIPT_DIR}/api/email_templates.py" "${DEPLOY_DIR}/"
cp "${SCRIPT_DIR}/api/email_sender.py" "${DEPLOY_DIR}/"
(cd "${DEPLOY_DIR}" && zip -q -r price-history-api.zip lambda_function.py email_templates.py email_sender.py)
echo "  Packaged: lambda_function.py, email_templates.py, email_sender.py"

# ----------------------------------------------------------------
# Step 3: Deploy Lambda
# ----------------------------------------------------------------
echo ""
echo "[3/5] Deploying Lambda..."

ENV_VARS="Variables={PROXY_ENDPOINT=${PROXY_ENDPOINT},DB_USER=${DB_USER},DB_PASSWORD=${DB_PASSWORD},DATABASE_NAME=${DB_NAME},DB_PORT=5432,ADMIN_SECRET=${ADMIN_SECRET},SES_ENABLED=${SES_ENABLED},SES_SANDBOX_MODE=${SES_SANDBOX_MODE}}"

if aws lambda get-function --function-name "${FUNCTION_NAME}" > /dev/null 2>&1; then
    aws lambda update-function-code \
        --function-name "${FUNCTION_NAME}" \
        --zip-file "fileb://${DEPLOY_DIR}/price-history-api.zip" \
        --output text > /dev/null
    echo "  Updated existing Lambda code"

    aws lambda wait function-updated-v2 --function-name "${FUNCTION_NAME}"

    aws lambda update-function-configuration \
        --function-name "${FUNCTION_NAME}" \
        --timeout 30 \
        --memory-size 128 \
        --layers "${LAYER_ARN}" \
        --vpc-config "SubnetIds=${PUBLIC_SUBNET_IDS},SecurityGroupIds=${SECURITY_GROUP_IDS}" \
        --environment "${ENV_VARS}" \
        --output text > /dev/null
    echo "  Updated Lambda config"
else
    aws lambda create-function \
        --function-name "${FUNCTION_NAME}" \
        --runtime python3.12 \
        --handler lambda_function.lambda_handler \
        --role "${ROLE_ARN_FULL}" \
        --zip-file "fileb://${DEPLOY_DIR}/price-history-api.zip" \
        --timeout 30 \
        --memory-size 128 \
        --architectures arm64 \
        --layers "${LAYER_ARN}" \
        --vpc-config "SubnetIds=${PUBLIC_SUBNET_IDS},SecurityGroupIds=${SECURITY_GROUP_IDS}" \
        --environment "${ENV_VARS}" \
        --output text > /dev/null
    echo "  Created Lambda: ${FUNCTION_NAME}"
fi

aws lambda wait function-active-v2 --function-name "${FUNCTION_NAME}"
echo "  Lambda is active"

rm -rf "${DEPLOY_DIR}"

# ----------------------------------------------------------------
# Step 4: Create API Gateway HTTP API (public)
# ----------------------------------------------------------------
echo ""
echo "[4/5] Creating API Gateway..."

API_NAME="price-history-api"
LAMBDA_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${FUNCTION_NAME}"

# Check if API already exists
API_ID=$(aws apigatewayv2 get-apis --query "Items[?Name=='${API_NAME}'].ApiId | [0]" --output text 2>/dev/null || echo "None")

if [ "${API_ID}" = "None" ] || [ -z "${API_ID}" ]; then
    API_ID=$(aws apigatewayv2 create-api \
        --name "${API_NAME}" \
        --protocol-type HTTP \
        --cors-configuration '{
            "AllowOrigins": ["*"],
            "AllowMethods": ["GET", "POST", "OPTIONS"],
            "AllowHeaders": ["Content-Type", "X-Admin-Secret"],
            "MaxAge": 86400
        }' \
        --query 'ApiId' --output text)
    echo "  Created HTTP API: ${API_ID}"

    INTEGRATION_ID=$(aws apigatewayv2 create-integration \
        --api-id "${API_ID}" \
        --integration-type AWS_PROXY \
        --integration-uri "${LAMBDA_ARN}" \
        --payload-format-version "2.0" \
        --query 'IntegrationId' --output text)

    aws apigatewayv2 create-route \
        --api-id "${API_ID}" \
        --route-key "GET /prices" \
        --target "integrations/${INTEGRATION_ID}" \
        --output text > /dev/null
    aws apigatewayv2 create-route \
        --api-id "${API_ID}" \
        --route-key "GET /skus" \
        --target "integrations/${INTEGRATION_ID}" \
        --output text > /dev/null
    aws apigatewayv2 create-route \
        --api-id "${API_ID}" \
        --route-key "GET /catalog" \
        --target "integrations/${INTEGRATION_ID}" \
        --output text > /dev/null
    aws apigatewayv2 create-route \
        --api-id "${API_ID}" \
        --route-key "GET /indices" \
        --target "integrations/${INTEGRATION_ID}" \
        --output text > /dev/null
    echo "  Routes: GET /prices, GET /skus, GET /catalog, GET /indices"

    aws apigatewayv2 create-stage \
        --api-id "${API_ID}" \
        --stage-name '$default' \
        --auto-deploy \
        --output text > /dev/null
    echo "  Stage: \$default (auto-deploy)"

    aws lambda add-permission \
        --function-name "${FUNCTION_NAME}" \
        --statement-id "apigateway-invoke" \
        --action "lambda:InvokeFunction" \
        --principal "apigateway.amazonaws.com" \
        --source-arn "arn:aws:execute-api:${REGION}:${ACCOUNT_ID}:${API_ID}/*" \
        --output text > /dev/null 2>&1 || true
    echo "  Lambda permission granted"
else
    echo "  API already exists: ${API_ID}"

    # Ensure routes exist
    EXISTING_ROUTES=$(aws apigatewayv2 get-routes --api-id "${API_ID}" --query "Items[].RouteKey" --output text 2>/dev/null || echo "")
    INTEGRATION_ID=$(aws apigatewayv2 get-integrations --api-id "${API_ID}" --query "Items[0].IntegrationId" --output text)

    for ROUTE_KEY in "GET /catalog" "GET /indices" "GET /buylist" "POST /tradein" "GET /tradein/{reference}" "POST /tradein/{reference}/status"; do
        if echo "${EXISTING_ROUTES}" | grep -q "${ROUTE_KEY}"; then
            echo "  Route ${ROUTE_KEY} already exists"
        else
            aws apigatewayv2 create-route \
                --api-id "${API_ID}" \
                --route-key "${ROUTE_KEY}" \
                --target "integrations/${INTEGRATION_ID}" \
                --output text > /dev/null
            echo "  Added route: ${ROUTE_KEY}"
        fi
    done

    # Update CORS to allow POST + X-Admin-Secret
    aws apigatewayv2 update-api \
        --api-id "${API_ID}" \
        --cors-configuration '{
            "AllowOrigins": ["*"],
            "AllowMethods": ["GET", "POST", "OPTIONS"],
            "AllowHeaders": ["Content-Type", "X-Admin-Secret"],
            "MaxAge": 86400
        }' \
        --output text > /dev/null
    echo "  CORS updated (GET, POST, OPTIONS + X-Admin-Secret)"
fi

API_URL="https://${API_ID}.execute-api.${REGION}.amazonaws.com"

# ----------------------------------------------------------------
# Step 5: Smoke test
# ----------------------------------------------------------------
echo ""
echo "[5/5] Running smoke test..."

sleep 3
HTTP_CODE=$(curl -s -o /tmp/price-history-api-test.json -w '%{http_code}' \
    "${API_URL}/prices?sku=OP-OP01-001-JP&days=7" 2>&1)

if [ "${HTTP_CODE}" = "200" ]; then
    BODY=$(python3 -c "import json; d=json.load(open('/tmp/price-history-api-test.json')); print(f\"{d['count']} price points returned for {d['sku']}\")" 2>/dev/null || echo "response received")
    echo "  Smoke test passed (HTTP ${HTTP_CODE}): ${BODY}"
else
    echo "  WARNING: HTTP ${HTTP_CODE}"
fi

# ----------------------------------------------------------------
# Done
# ----------------------------------------------------------------
echo ""
echo "============================================================"
echo "Deployment complete!"
echo ""
echo "Resources created/updated:"
echo "  - IAM Role: ${ROLE_NAME}"
echo "  - Lambda: ${FUNCTION_NAME} (30s timeout, 128MB, public VPC)"
echo "  - API Gateway: ${API_URL}"
echo ""
echo "API usage:"
echo "  GET ${API_URL}/prices?sku=OP-OP01-001-JP"
echo "  GET ${API_URL}/prices?sku=OP-OP01-001-JP&days=90"
echo "  GET ${API_URL}/skus"
echo "  GET ${API_URL}/catalog"
echo "  GET ${API_URL}/indices?days=30"
echo "  GET ${API_URL}/tradein/TI-XXXXXXXX-XXXX?email=user@example.com"
echo ""
echo "Admin (requires ADMIN_SECRET):"
echo "  curl -X POST ${API_URL}/tradein/TI-XXXX/status \\"
echo "    -H 'X-Admin-Secret: \$ADMIN_SECRET' \\"
echo "    -d '{\"status\":\"received\"}'"
echo ""
echo "Add to Shopify product page:"
echo "  1. Theme Editor → Product template → Add section → Price History"
echo "  2. Set API URL to: ${API_URL}"
echo "============================================================"
