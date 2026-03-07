#!/bin/bash
set -euo pipefail

# ============================================================================
# Shopify Price Push Lambda — AWS Deployment Script
#
# Creates: IAM role, Lambda function in private VPC subnets (with NAT).
# Uses: RDS Proxy for price data, Shopify Admin API for price updates.
#
# Prerequisites:
#   - AWS CLI configured
#   - NAT Gateway available in private subnets
#   - .env file with DB credentials + SHOPIFY_API_PASSWORD
#
# Usage: bash pricing/deploy_shopify_push.sh
# ============================================================================

REGION="us-east-1"
ACCOUNT_ID="034362054546"

# --- Infrastructure ---
# Private subnets with NAT Gateway routing (internet for Shopify API)
PRIVATE_SUBNET_IDS="subnet-036f2976eb614c5aa,subnet-08810fbbf412af6a7"
# health-check SG (RDS Proxy access on port 5432)
SECURITY_GROUP_IDS="sg-00866258c72d6b39d"
LAYER_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:layer:price-scraper-py312:1"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# --- Config (from .env or environment) ---
if [ -f "${REPO_ROOT}/.env" ]; then
    source "${REPO_ROOT}/.env"
fi
: "${PROXY_ENDPOINT:?ERROR: PROXY_ENDPOINT not set. Copy .env.example to .env and fill values.}"
: "${DB_USER:?ERROR: DB_USER not set.}"
: "${DB_PASSWORD:?ERROR: DB_PASSWORD not set.}"
: "${SHOPIFY_API_PASSWORD:?ERROR: SHOPIFY_API_PASSWORD not set.}"
DB_NAME="${DATABASE_NAME:-op_cardrush_link}"
TABLE_NAME="${TABLE_NAME:-cardrush_link}"
SHOPIFY_STORE="${SHOPIFY_STORE:-6e824e-a9.myshopify.com}"
SHOPIFY_API_VERSION="${SHOPIFY_API_VERSION:-2025-01}"

# --- Naming ---
FUNCTION_NAME="shopify-price-push"
ROLE_NAME="shopify-price-push-role"

echo "============================================================"
echo "Shopify Price Push Lambda — AWS Deployment"
echo "Region: ${REGION}"
echo "Account: ${ACCOUNT_ID}"
echo "============================================================"

# ----------------------------------------------------------------
# Step 0: Validate
# ----------------------------------------------------------------
echo ""
echo "[0/4] Validating AWS credentials..."
aws sts get-caller-identity --output text > /dev/null
echo "  OK"

# ----------------------------------------------------------------
# Step 1: Create IAM role
# ----------------------------------------------------------------
echo ""
echo "[1/4] Creating IAM role..."

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
        --description "Shopify price push Lambda role" \
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
    --policy-name "shopify-push-permissions" \
    --policy-document "${INLINE_POLICY}"
echo "  Inline policy attached (logs)"

# ----------------------------------------------------------------
# Step 2: Package Lambda
# ----------------------------------------------------------------
echo ""
echo "[2/4] Packaging Lambda..."

DEPLOY_DIR=$(mktemp -d)
cp "${SCRIPT_DIR}/push/shopify/lambda_function.py" "${DEPLOY_DIR}/"

# Create monitoring package for 'from monitoring.metrics import record_pipeline_run'
mkdir -p "${DEPLOY_DIR}/monitoring"
touch "${DEPLOY_DIR}/monitoring/__init__.py"
cp "${SCRIPT_DIR}/monitoring/metrics.py" "${DEPLOY_DIR}/monitoring/"

(cd "${DEPLOY_DIR}" && zip -q -r shopify-push.zip lambda_function.py monitoring/)
echo "  Packaged: lambda_function.py, monitoring/metrics.py"

# ----------------------------------------------------------------
# Step 3: Deploy Lambda
# ----------------------------------------------------------------
echo ""
echo "[3/4] Deploying Lambda..."

if aws lambda get-function --function-name "${FUNCTION_NAME}" > /dev/null 2>&1; then
    aws lambda update-function-code \
        --function-name "${FUNCTION_NAME}" \
        --zip-file "fileb://${DEPLOY_DIR}/shopify-push.zip" \
        --output text > /dev/null
    echo "  Updated existing Lambda code"

    aws lambda wait function-updated-v2 --function-name "${FUNCTION_NAME}"

    aws lambda update-function-configuration \
        --function-name "${FUNCTION_NAME}" \
        --timeout 600 \
        --memory-size 256 \
        --layers "${LAYER_ARN}" \
        --vpc-config "SubnetIds=${PRIVATE_SUBNET_IDS},SecurityGroupIds=${SECURITY_GROUP_IDS}" \
        --environment "Variables={PROXY_ENDPOINT=${PROXY_ENDPOINT},DB_USER=${DB_USER},DB_PASSWORD=${DB_PASSWORD},DATABASE_NAME=${DB_NAME},TABLE_NAME=${TABLE_NAME},DB_PORT=5432,SHOPIFY_STORE=${SHOPIFY_STORE},SHOPIFY_API_PASSWORD=${SHOPIFY_API_PASSWORD},SHOPIFY_API_VERSION=${SHOPIFY_API_VERSION}}" \
        --output text > /dev/null
    echo "  Updated Lambda config"
else
    aws lambda create-function \
        --function-name "${FUNCTION_NAME}" \
        --runtime python3.12 \
        --handler lambda_function.lambda_handler \
        --role "${ROLE_ARN_FULL}" \
        --zip-file "fileb://${DEPLOY_DIR}/shopify-push.zip" \
        --timeout 600 \
        --memory-size 256 \
        --architectures arm64 \
        --layers "${LAYER_ARN}" \
        --vpc-config "SubnetIds=${PRIVATE_SUBNET_IDS},SecurityGroupIds=${SECURITY_GROUP_IDS}" \
        --environment "Variables={PROXY_ENDPOINT=${PROXY_ENDPOINT},DB_USER=${DB_USER},DB_PASSWORD=${DB_PASSWORD},DATABASE_NAME=${DB_NAME},TABLE_NAME=${TABLE_NAME},DB_PORT=5432,SHOPIFY_STORE=${SHOPIFY_STORE},SHOPIFY_API_PASSWORD=${SHOPIFY_API_PASSWORD},SHOPIFY_API_VERSION=${SHOPIFY_API_VERSION}}" \
        --output text > /dev/null
    echo "  Created Lambda: ${FUNCTION_NAME}"
fi

aws lambda wait function-active-v2 --function-name "${FUNCTION_NAME}"
echo "  Lambda is active"

rm -rf "${DEPLOY_DIR}"

# ----------------------------------------------------------------
# Step 4: Verify (no invocation — requires confirmation)
# ----------------------------------------------------------------
echo ""
echo "[4/4] Verifying deployment..."
aws lambda get-function-configuration \
    --function-name "${FUNCTION_NAME}" \
    --query '{Runtime:Runtime,Timeout:Timeout,MemorySize:MemorySize,State:State}' \
    --output table
echo "  Deployment verified"

# ----------------------------------------------------------------
# Done
# ----------------------------------------------------------------
echo ""
echo "============================================================"
echo "Deployment complete!"
echo ""
echo "Resources created/updated:"
echo "  - IAM Role: ${ROLE_NAME}"
echo "  - Lambda: ${FUNCTION_NAME} (600s timeout, 256MB, private VPC)"
echo "  - Subnets: ${PRIVATE_SUBNET_IDS} (NAT Gateway routing)"
echo "  - SGs: ${SECURITY_GROUP_IDS}"
echo ""
echo "To push prices:"
echo "  aws lambda invoke --function-name ${FUNCTION_NAME} --cli-binary-format raw-in-base64-out /tmp/shopify-push-result.json && cat /tmp/shopify-push-result.json"
echo "============================================================"
