#!/bin/bash
set -euo pipefail

# ============================================================================
# Run a SQL migration file via a temporary Lambda (VPC-only RDS access).
#
# Usage: bash pricing/run_migration.sh pricing/migrations/004_add_sales_events.sql
# ============================================================================

if [ $# -lt 1 ]; then
    echo "Usage: $0 <migration_file.sql>"
    exit 1
fi

MIGRATION_FILE="$1"
if [ ! -f "${MIGRATION_FILE}" ]; then
    echo "ERROR: File not found: ${MIGRATION_FILE}"
    exit 1
fi

REGION="us-east-1"
ACCOUNT_ID="034362054546"
FUNCTION_NAME="db-migration-runner"
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/stock-inventory-api-role"
PUBLIC_SUBNET_IDS="subnet-01095623139ea0f77,subnet-05f2d8747e37bf970,subnet-0d846dd0951910224,subnet-03b96e0280a920cf0,subnet-0615ce339a1e17101,subnet-04dd86b8074d9055f"
SECURITY_GROUP_IDS="sg-00866258c72d6b39d"
LAYER_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:layer:price-scraper-py312:1"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Load DB config
if [ -f "${REPO_ROOT}/.env" ]; then
    source "${REPO_ROOT}/.env"
fi
: "${PROXY_ENDPOINT:?ERROR: PROXY_ENDPOINT not set.}"
: "${DB_USER:?ERROR: DB_USER not set.}"
: "${DB_PASSWORD:?ERROR: DB_PASSWORD not set.}"
DB_NAME="${DATABASE_NAME:-op_cardrush_link}"

echo "=== Migration Runner ==="
echo "File: ${MIGRATION_FILE}"
echo ""

# --- Build temp Lambda ---
DEPLOY_DIR=$(mktemp -d)
cat > "${DEPLOY_DIR}/lambda_function.py" << 'PYEOF'
import json, os, psycopg2

def lambda_handler(event, context):
    sql = event.get("sql", "")
    if not sql:
        return {"statusCode": 400, "body": "No SQL provided"}
    conn = psycopg2.connect(
        host=os.environ["PROXY_ENDPOINT"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ.get("DATABASE_NAME", "op_cardrush_link"),
        connect_timeout=10,
    )
    try:
        cur = conn.cursor()
        cur.execute(sql)
        conn.commit()
        status = cur.statusmessage
        cur.close()
        return {"statusCode": 200, "body": json.dumps({"status": status})}
    except Exception as e:
        conn.rollback()
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}
    finally:
        conn.close()
PYEOF
(cd "${DEPLOY_DIR}" && zip -q -r migration.zip lambda_function.py)

# --- Deploy ---
ENV_VARS="Variables={PROXY_ENDPOINT=${PROXY_ENDPOINT},DB_USER=${DB_USER},DB_PASSWORD=${DB_PASSWORD},DATABASE_NAME=${DB_NAME},DB_PORT=5432}"

if aws lambda get-function --function-name "${FUNCTION_NAME}" > /dev/null 2>&1; then
    aws lambda update-function-code \
        --function-name "${FUNCTION_NAME}" \
        --zip-file "fileb://${DEPLOY_DIR}/migration.zip" \
        --output text > /dev/null
    aws lambda wait function-updated-v2 --function-name "${FUNCTION_NAME}"
    aws lambda update-function-configuration \
        --function-name "${FUNCTION_NAME}" \
        --timeout 30 --memory-size 128 \
        --layers "${LAYER_ARN}" \
        --vpc-config "SubnetIds=${PUBLIC_SUBNET_IDS},SecurityGroupIds=${SECURITY_GROUP_IDS}" \
        --environment "${ENV_VARS}" \
        --output text > /dev/null
else
    aws lambda create-function \
        --function-name "${FUNCTION_NAME}" \
        --runtime python3.12 \
        --handler lambda_function.lambda_handler \
        --role "${ROLE_ARN}" \
        --zip-file "fileb://${DEPLOY_DIR}/migration.zip" \
        --timeout 30 --memory-size 128 \
        --architectures arm64 \
        --layers "${LAYER_ARN}" \
        --vpc-config "SubnetIds=${PUBLIC_SUBNET_IDS},SecurityGroupIds=${SECURITY_GROUP_IDS}" \
        --environment "${ENV_VARS}" \
        --output text > /dev/null
    echo "  Created Lambda: ${FUNCTION_NAME}"
fi

aws lambda wait function-active-v2 --function-name "${FUNCTION_NAME}"
echo "  Migration runner ready"

# --- Read SQL and invoke ---
SQL_CONTENT=$(cat "${MIGRATION_FILE}")
PAYLOAD=$(python3 -c "import json; print(json.dumps({'sql': open('${MIGRATION_FILE}').read()}))")

echo "  Running migration..."
aws lambda invoke \
    --function-name "${FUNCTION_NAME}" \
    --payload "${PAYLOAD}" \
    --cli-binary-format raw-in-base64-out \
    /tmp/migration-result.json \
    --output text > /dev/null

RESULT=$(cat /tmp/migration-result.json)
echo "  Result: ${RESULT}"

# --- Cleanup ---
rm -rf "${DEPLOY_DIR}"

echo ""
echo "=== Migration complete ==="
echo "Note: ${FUNCTION_NAME} Lambda left in place for future migrations."
echo "  Delete with: aws lambda delete-function --function-name ${FUNCTION_NAME}"
