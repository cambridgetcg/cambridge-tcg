#!/bin/bash
set -euo pipefail

# ============================================================================
# Stock Inventory API Lambda — AWS Deployment Script
#
# Creates: IAM role, Lambda function in public VPC subnets, Function URL.
# Uses: RDS Proxy for stock data, API key auth via x-api-key header.
#
# Prerequisites:
#   - AWS CLI configured
#   - .env file with DB credentials
#
# Usage: bash stock/inventory/deploy.sh
# ============================================================================

REGION="us-east-1"
ACCOUNT_ID="034362054546"

# --- Infrastructure ---
# Public subnets (same as health-check/scraper — only needs RDS Proxy, no internet)
PUBLIC_SUBNET_IDS="subnet-01095623139ea0f77,subnet-05f2d8747e37bf970,subnet-0d846dd0951910224,subnet-03b96e0280a920cf0,subnet-0615ce339a1e17101,subnet-04dd86b8074d9055f"
# Health-check SG (has RDS Proxy egress on 5432)
SECURITY_GROUP_IDS="sg-00866258c72d6b39d"
LAYER_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:layer:price-scraper-py312:1"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# --- DB config (from .env or environment) ---
if [ -f "${REPO_ROOT}/.env" ]; then
    source "${REPO_ROOT}/.env"
fi
: "${PROXY_ENDPOINT:?ERROR: PROXY_ENDPOINT not set. Copy .env.example to .env and fill values.}"
: "${DB_USER:?ERROR: DB_USER not set.}"
: "${DB_PASSWORD:?ERROR: DB_PASSWORD not set.}"
DB_NAME="${DATABASE_NAME:-op_cardrush_link}"
TABLE_NAME="${TABLE_NAME:-cardrush_link}"

# --- Naming ---
FUNCTION_NAME="stock-inventory-api"
ROLE_NAME="stock-inventory-api-role"

echo "============================================================"
echo "Stock Inventory API Lambda — AWS Deployment"
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
        --description "Stock inventory API Lambda role" \
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
    }
  ]
}
EOF
)

aws iam put-role-policy \
    --role-name "${ROLE_NAME}" \
    --policy-name "stock-inventory-api-permissions" \
    --policy-document "${INLINE_POLICY}"
echo "  Inline policy attached (CloudWatch Logs)"

# ----------------------------------------------------------------
# Step 2: Package Lambda
# ----------------------------------------------------------------
echo ""
echo "[2/5] Packaging Lambda..."

DEPLOY_DIR=$(mktemp -d)
cp "${SCRIPT_DIR}/lambda_function.py" "${DEPLOY_DIR}/"
(cd "${DEPLOY_DIR}" && zip -q -r stock-inventory-api.zip lambda_function.py)
echo "  Packaged: lambda_function.py"

# ----------------------------------------------------------------
# Step 3: Deploy Lambda
# ----------------------------------------------------------------
echo ""
echo "[3/5] Deploying Lambda..."

ENV_VARS="Variables={PROXY_ENDPOINT=${PROXY_ENDPOINT},DB_USER=${DB_USER},DB_PASSWORD=${DB_PASSWORD},DATABASE_NAME=${DB_NAME},TABLE_NAME=${TABLE_NAME},DB_PORT=5432}"

if aws lambda get-function --function-name "${FUNCTION_NAME}" > /dev/null 2>&1; then
    aws lambda update-function-code \
        --function-name "${FUNCTION_NAME}" \
        --zip-file "fileb://${DEPLOY_DIR}/stock-inventory-api.zip" \
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
        --zip-file "fileb://${DEPLOY_DIR}/stock-inventory-api.zip" \
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
# Step 4: Smoke test (direct invoke via boto3/CLI)
# ----------------------------------------------------------------
echo ""
echo "[4/4] Running smoke test..."

INVOKE_STATUS=$(aws lambda invoke \
    --function-name "${FUNCTION_NAME}" \
    --payload '{"requestContext":{"http":{"method":"GET","path":"/inventory"}},"headers":{}}' \
    --cli-binary-format raw-in-base64-out \
    /tmp/stock-api-test.json \
    --query 'StatusCode' --output text 2>&1)

if [ "${INVOKE_STATUS}" = "200" ]; then
    BODY=$(python3 -c "import json; d=json.load(open('/tmp/stock-api-test.json')); b=json.loads(d['body']); print(f\"{b['count']} SKUs returned\")" 2>/dev/null || echo "response received")
    echo "  Smoke test passed: ${BODY}"
else
    echo "  WARNING: Invoke returned status ${INVOKE_STATUS}"
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
echo ""
echo "Access: Streamlit app uses boto3 lambda.invoke() (IAM auth)"
echo "  No Function URL or API key needed."
echo ""
echo "Run Streamlit:"
echo "  pip install -r stock/inventory/requirements.txt"
echo "  streamlit run stock/inventory/app.py"
echo "============================================================"
