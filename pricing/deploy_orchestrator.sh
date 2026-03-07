#!/bin/bash
set -euo pipefail

# ============================================================================
# Pipeline Orchestrator — AWS Deployment Script
#
# Creates: IAM role, orchestrator Lambda, EventBridge daily schedule.
# Reuses: VPC, subnets, security group, RDS Proxy from existing infra.
#
# Prerequisites:
#   - AWS CLI configured (aws sts get-caller-identity works)
#   - Existing Lambdas: cardrush_scraper, cardrush-fx-updater, price_calculator,
#     api-shopify, api-ebay, pipeline-health-check
#
# Usage: bash pricing/deploy_orchestrator.sh
# ============================================================================

REGION="us-east-1"
ACCOUNT_ID="034362054546"

# --- Infrastructure from existing Lambdas ---
VPC_ID="vpc-073cdce8e84cbccdc"
SUBNET_IDS="subnet-01095623139ea0f77,subnet-05f2d8747e37bf970,subnet-0d846dd0951910224,subnet-03b96e0280a920cf0,subnet-0615ce339a1e17101,subnet-04dd86b8074d9055f"
# Reuse health-check SG (RDS + CloudWatch egress)
HEALTH_CHECK_SG="sg-00866258c72d6b39d"
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
DB_NAME="${DATABASE_NAME:-op_cardrush_link}"
TABLE_NAME="${TABLE_NAME:-cardrush_link}"

# --- SNS ---
SNS_TOPIC_ARN="arn:aws:sns:${REGION}:${ACCOUNT_ID}:cambridge-tcg-pipeline-alerts"

# --- Naming ---
FUNCTION_NAME="pipeline-orchestrator"
ROLE_NAME="pipeline-orchestrator-role"
RULE_NAME="pipeline-orchestrator-daily"

# Lambda ARNs to invoke
LAMBDA_ARNS=(
    "arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:cardrush_scraper"
    "arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:get_GBP-JPY"
    "arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:price_calculator"
    "arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:API_shopify"
    "arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:ebay-price-push"
    "arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:pipeline-health-check"
)

echo "============================================================"
echo "Pipeline Orchestrator — AWS Deployment"
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
        --description "Pipeline orchestrator Lambda role" \
        --query 'Role.Arn' --output text)
    echo "  Created role: ${ROLE_ARN_FULL}"

    # Wait for role to propagate
    echo "  Waiting for role propagation..."
    sleep 10
fi

# Attach VPC access policy (for RDS connection)
aws iam attach-role-policy \
    --role-name "${ROLE_NAME}" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole \
    2>/dev/null || true
echo "  VPC access policy attached"

# Create inline policy for Lambda invoke + SNS publish
INVOKE_RESOURCES=$(printf '"%s",' "${LAMBDA_ARNS[@]}")
INVOKE_RESOURCES="[${INVOKE_RESOURCES%,}]"

INLINE_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "lambda:InvokeFunction",
      "Resource": ${INVOKE_RESOURCES}
    },
    {
      "Effect": "Allow",
      "Action": "sns:Publish",
      "Resource": "${SNS_TOPIC_ARN}"
    },
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
    --policy-name "orchestrator-permissions" \
    --policy-document "${INLINE_POLICY}"
echo "  Inline policy attached (lambda:InvokeFunction + sns:Publish + logs)"

# ----------------------------------------------------------------
# Step 2: Package and deploy Lambda
# ----------------------------------------------------------------
echo ""
echo "[2/5] Deploying orchestrator Lambda..."

DEPLOY_DIR=$(mktemp -d)
cp "${SCRIPT_DIR}/orchestrator/lambda_function.py" "${DEPLOY_DIR}/"
cp "${SCRIPT_DIR}/monitoring/metrics.py" "${DEPLOY_DIR}/"
(cd "${DEPLOY_DIR}" && zip -q orchestrator.zip lambda_function.py metrics.py)

if aws lambda get-function --function-name "${FUNCTION_NAME}" > /dev/null 2>&1; then
    aws lambda update-function-code \
        --function-name "${FUNCTION_NAME}" \
        --zip-file "fileb://${DEPLOY_DIR}/orchestrator.zip" \
        --output text > /dev/null
    echo "  Updated existing Lambda code"

    aws lambda wait function-updated-v2 --function-name "${FUNCTION_NAME}"

    aws lambda update-function-configuration \
        --function-name "${FUNCTION_NAME}" \
        --timeout 900 \
        --memory-size 256 \
        --layers "${LAYER_ARN}" \
        --vpc-config "SubnetIds=${SUBNET_IDS},SecurityGroupIds=${HEALTH_CHECK_SG}" \
        --environment "Variables={PROXY_ENDPOINT=${PROXY_ENDPOINT},DB_USER=${DB_USER},DB_PASSWORD=${DB_PASSWORD},DATABASE_NAME=${DB_NAME},TABLE_NAME=${TABLE_NAME},DB_PORT=5432,SNS_TOPIC_ARN=${SNS_TOPIC_ARN}}" \
        --output text > /dev/null
    echo "  Updated Lambda config"
else
    aws lambda create-function \
        --function-name "${FUNCTION_NAME}" \
        --runtime python3.12 \
        --handler lambda_function.lambda_handler \
        --role "${ROLE_ARN_FULL}" \
        --zip-file "fileb://${DEPLOY_DIR}/orchestrator.zip" \
        --timeout 900 \
        --memory-size 256 \
        --architectures arm64 \
        --layers "${LAYER_ARN}" \
        --vpc-config "SubnetIds=${SUBNET_IDS},SecurityGroupIds=${HEALTH_CHECK_SG}" \
        --environment "Variables={PROXY_ENDPOINT=${PROXY_ENDPOINT},DB_USER=${DB_USER},DB_PASSWORD=${DB_PASSWORD},DATABASE_NAME=${DB_NAME},TABLE_NAME=${TABLE_NAME},DB_PORT=5432,SNS_TOPIC_ARN=${SNS_TOPIC_ARN}}" \
        --output text > /dev/null
    echo "  Created Lambda: ${FUNCTION_NAME}"
fi

aws lambda wait function-active-v2 --function-name "${FUNCTION_NAME}"
echo "  Lambda is active"

rm -rf "${DEPLOY_DIR}"

# ----------------------------------------------------------------
# Step 3: Create EventBridge rule (daily 06:00 UTC)
# ----------------------------------------------------------------
echo ""
echo "[3/5] Creating EventBridge schedule..."

FUNCTION_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${FUNCTION_NAME}"

aws events put-rule \
    --name "${RULE_NAME}" \
    --schedule-expression "cron(0 6 * * ? *)" \
    --state ENABLED \
    --description "Trigger pipeline orchestrator daily at 06:00 UTC" \
    --output text > /dev/null
echo "  Created/updated rule: ${RULE_NAME}"

# ----------------------------------------------------------------
# Step 4: Add Lambda permission for EventBridge
# ----------------------------------------------------------------
echo ""
echo "[4/5] Configuring EventBridge → Lambda permission..."

aws lambda add-permission \
    --function-name "${FUNCTION_NAME}" \
    --statement-id "EventBridgeDailyInvoke" \
    --action "lambda:InvokeFunction" \
    --principal "events.amazonaws.com" \
    --source-arn "arn:aws:events:${REGION}:${ACCOUNT_ID}:rule/${RULE_NAME}" \
    2>/dev/null || echo "  Permission already exists"

aws events put-targets \
    --rule "${RULE_NAME}" \
    --targets "Id=orchestrator-target,Arn=${FUNCTION_ARN}" \
    --output text > /dev/null
echo "  Linked rule to Lambda"

# ----------------------------------------------------------------
# Step 5: Smoke test (dry run)
# ----------------------------------------------------------------
echo ""
echo "[5/5] Running smoke test (dry run)..."

aws lambda wait function-active-v2 --function-name "${FUNCTION_NAME}" 2>/dev/null || true

INVOKE_STATUS=$(aws lambda invoke \
    --function-name "${FUNCTION_NAME}" \
    --payload '{"dry_run": true}' \
    --cli-binary-format raw-in-base64-out \
    /tmp/orchestrator-result.json \
    --query 'StatusCode' --output text 2>&1)

if [ "${INVOKE_STATUS}" = "200" ]; then
    RESULT_BODY=$(cat /tmp/orchestrator-result.json)
    echo "  Dry run result: ${RESULT_BODY}"
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
echo "  - Lambda: ${FUNCTION_NAME} (600s timeout, 256MB)"
echo "  - EventBridge: ${RULE_NAME} (daily 06:00 UTC)"
echo ""
echo "To test manually:"
echo "  aws lambda invoke --function-name ${FUNCTION_NAME} --payload '{\"dry_run\": true}' --cli-binary-format raw-in-base64-out /dev/stdout"
echo ""
echo "To run full pipeline:"
echo "  aws lambda invoke --function-name ${FUNCTION_NAME} /dev/stdout"
echo ""
echo "To start from a specific stage:"
echo "  aws lambda invoke --function-name ${FUNCTION_NAME} --payload '{\"start_from\": \"calculator\"}' --cli-binary-format raw-in-base64-out /dev/stdout"
echo "============================================================"
