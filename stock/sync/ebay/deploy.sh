#!/bin/bash
set -euo pipefail

# ============================================================================
# eBay Metadata Sync — AWS Lambda Deployment Script
#
# Creates: IAM role, Lambda function, EventBridge schedule (disabled).
# No VPC — this Lambda only calls eBay API (api.ebay.com) and Secrets Manager.
#
# Prerequisites:
#   - AWS CLI configured (aws sts get-caller-identity works)
#   - pip available (for installing requests into zip)
#
# Usage: bash stock/sync/ebay/deploy.sh
# ============================================================================

REGION="us-east-1"
ACCOUNT_ID="034362054546"

# --- Naming ---
FUNCTION_NAME="ebay-metadata-sync"
ROLE_NAME="ebay-metadata-sync-role"
RULE_NAME="ebay-metadata-sync-schedule"
SECRET_ARN="arn:aws:secretsmanager:${REGION}:${ACCOUNT_ID}:secret:ebay-trading-api-credentials"

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"

echo "============================================================"
echo "eBay Metadata Sync — Lambda Deployment"
echo "Region: ${REGION}"
echo "Account: ${ACCOUNT_ID}"
echo "============================================================"

# ----------------------------------------------------------------
# Step 0: Validate AWS credentials
# ----------------------------------------------------------------
echo ""
echo "[0/4] Validating AWS credentials..."
aws sts get-caller-identity --output text > /dev/null
echo "  OK"

# ----------------------------------------------------------------
# Step 1: Create IAM role (idempotent)
# ----------------------------------------------------------------
echo ""
echo "[1/4] Ensuring IAM role exists..."

TRUST_POLICY='{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "lambda.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }
  ]
}'

SECRETS_POLICY='{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:us-east-1:034362054546:secret:ebay-trading-api-credentials*"
    }
  ]
}'

ROLE_CREATED=false

if aws iam get-role --role-name "${ROLE_NAME}" > /dev/null 2>&1; then
    echo "  Role already exists: ${ROLE_NAME}"
    ROLE_ARN=$(aws iam get-role --role-name "${ROLE_NAME}" --query 'Role.Arn' --output text)
else
    ROLE_ARN=$(aws iam create-role \
        --role-name "${ROLE_NAME}" \
        --assume-role-policy-document "${TRUST_POLICY}" \
        --description "eBay metadata sync Lambda - eBay API + Secrets Manager only" \
        --query 'Role.Arn' --output text)
    echo "  Created role: ${ROLE_ARN}"
    ROLE_CREATED=true
fi

# Attach managed policy: basic Lambda execution (CloudWatch Logs)
aws iam attach-role-policy \
    --role-name "${ROLE_NAME}" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole \
    2>/dev/null || true
echo "  AWSLambdaBasicExecutionRole attached"

# Inline policy: Secrets Manager access
aws iam put-role-policy \
    --role-name "${ROLE_NAME}" \
    --policy-name "SecretsManagerAccess" \
    --policy-document "${SECRETS_POLICY}"
echo "  SecretsManagerAccess inline policy attached"

if [ "${ROLE_CREATED}" = true ]; then
    echo "  Waiting 10s for IAM propagation..."
    sleep 10
fi

# ----------------------------------------------------------------
# Step 2: Package Lambda zip
# ----------------------------------------------------------------
echo ""
echo "[2/4] Packaging Lambda zip..."

TMPDIR=$(mktemp -d)
trap 'rm -rf "${TMPDIR}"' EXIT

# Mirror repo directory structure
mkdir -p "${TMPDIR}/stock/sync/ebay"
mkdir -p "${TMPDIR}/pricing/push/ebay"

# Copy Lambda code (exclude CLI-only files)
for f in __init__.py lambda_function.py client.py sync.py normalizer.py description.py item_specifics.py; do
    cp "${REPO_ROOT}/stock/sync/ebay/${f}" "${TMPDIR}/stock/sync/ebay/"
done

# Package __init__.py for parent packages
cp "${REPO_ROOT}/stock/__init__.py" "${TMPDIR}/stock/"
cp "${REPO_ROOT}/stock/sync/__init__.py" "${TMPDIR}/stock/sync/"

# Cross-package dependency: ebay_auth (bare module import via sys.path hack)
cp "${REPO_ROOT}/pricing/push/ebay/ebay_auth.py" "${TMPDIR}/pricing/push/ebay/"

# Install requests (+ transitive deps) into zip root
pip3 install requests -t "${TMPDIR}" --quiet --disable-pip-version-check 2>&1 | grep -v "already satisfied" || true

# Remove unnecessary pip metadata to shrink zip
rm -rf "${TMPDIR}"/*.dist-info "${TMPDIR}"/bin

ZIP_PATH="/tmp/ebay-metadata-sync.zip"
(cd "${TMPDIR}" && zip -qr "${ZIP_PATH}" .)

ZIP_SIZE=$(du -h "${ZIP_PATH}" | cut -f1)
echo "  Zip created: ${ZIP_PATH} (${ZIP_SIZE})"

# ----------------------------------------------------------------
# Step 3: Deploy Lambda (create or update)
# ----------------------------------------------------------------
echo ""
echo "[3/4] Deploying Lambda function..."

LAMBDA_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${FUNCTION_NAME}"

if aws lambda get-function --function-name "${FUNCTION_NAME}" > /dev/null 2>&1; then
    aws lambda update-function-code \
        --function-name "${FUNCTION_NAME}" \
        --zip-file "fileb://${ZIP_PATH}" \
        --output text > /dev/null
    echo "  Updated Lambda code"

    aws lambda wait function-updated-v2 --function-name "${FUNCTION_NAME}"

    aws lambda update-function-configuration \
        --function-name "${FUNCTION_NAME}" \
        --timeout 600 \
        --memory-size 256 \
        --environment "Variables={EBAY_SECRET_NAME=ebay-trading-api-credentials}" \
        --output text > /dev/null
    echo "  Updated Lambda configuration"
else
    aws lambda create-function \
        --function-name "${FUNCTION_NAME}" \
        --runtime python3.12 \
        --architectures arm64 \
        --handler stock.sync.ebay.lambda_function.lambda_handler \
        --role "${ROLE_ARN}" \
        --timeout 600 \
        --memory-size 256 \
        --zip-file "fileb://${ZIP_PATH}" \
        --environment "Variables={EBAY_SECRET_NAME=ebay-trading-api-credentials}" \
        --output text > /dev/null
    echo "  Created Lambda: ${FUNCTION_NAME}"
fi

aws lambda wait function-active-v2 --function-name "${FUNCTION_NAME}"
echo "  Lambda is active"

# ----------------------------------------------------------------
# Step 4: EventBridge schedule (disabled by default)
# ----------------------------------------------------------------
echo ""
echo "[4/4] Creating EventBridge schedule (disabled)..."

aws events put-rule \
    --name "${RULE_NAME}" \
    --schedule-expression "rate(7 days)" \
    --state DISABLED \
    --description "eBay metadata sync - weekly (enable manually when ready)" \
    --output text > /dev/null
echo "  Created/updated rule: ${RULE_NAME} (DISABLED)"

# Add Lambda permission for EventBridge
aws lambda add-permission \
    --function-name "${FUNCTION_NAME}" \
    --statement-id "EventBridgeInvoke" \
    --action "lambda:InvokeFunction" \
    --principal "events.amazonaws.com" \
    --source-arn "arn:aws:events:${REGION}:${ACCOUNT_ID}:rule/${RULE_NAME}" \
    2>/dev/null || echo "  Permission already exists"

aws events put-targets \
    --rule "${RULE_NAME}" \
    --targets "Id=ebay-sync-target,Arn=${LAMBDA_ARN}" \
    --output text > /dev/null
echo "  Linked rule to Lambda"

# ----------------------------------------------------------------
# Smoke test
# ----------------------------------------------------------------
echo ""
echo "============================================================"
echo "Deployment complete!"
echo ""
echo "Resources:"
echo "  - IAM Role: ${ROLE_NAME}"
echo "  - Lambda:   ${FUNCTION_NAME} (python3.12, arm64, 600s, 256MB)"
echo "  - Schedule: ${RULE_NAME} (DISABLED — rate(7 days))"
echo ""
echo "Smoke test (dry run):"
echo "  aws lambda invoke --function-name ${FUNCTION_NAME} \\"
echo "    --cli-binary-format raw-in-base64-out \\"
echo "    --payload '{\"dry_run\":true}' /tmp/ebay-sync-result.json"
echo "  cat /tmp/ebay-sync-result.json | python3 -m json.tool"
echo ""
echo "Title-only dry run:"
echo "  aws lambda invoke --function-name ${FUNCTION_NAME} \\"
echo "    --cli-binary-format raw-in-base64-out \\"
echo "    --payload '{\"dry_run\":true,\"sync_title\":true,\"sync_description\":false,\"sync_specifics\":false}' \\"
echo "    /tmp/ebay-sync-result.json"
echo ""
echo "Enable weekly schedule:"
echo "  aws events enable-rule --name ${RULE_NAME}"
echo "============================================================"
