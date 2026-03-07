#!/bin/bash
set -euo pipefail

# ============================================================================
# Pipeline Monitoring — AWS Deployment Script
#
# Creates: pipeline_runs table, health-check Lambda, EventBridge schedule,
#          SNS topic, CloudWatch alarms, VPC endpoint for CloudWatch,
#          dedicated security group, and updates existing Lambdas with
#          monitoring layer.
#
# Prerequisites:
#   - AWS CLI configured (aws sts get-caller-identity works)
#   - Existing Lambdas: cardrush_scraper, price_calculator
#
# Usage: bash pricing/deploy_monitoring.sh
# ============================================================================

REGION="us-east-1"
ACCOUNT_ID="034362054546"

# --- Infrastructure from existing Lambdas ---
VPC_ID="vpc-073cdce8e84cbccdc"
SUBNET_IDS="subnet-01095623139ea0f77,subnet-05f2d8747e37bf970,subnet-0d846dd0951910224,subnet-03b96e0280a920cf0,subnet-0615ce339a1e17101,subnet-04dd86b8074d9055f"
# Existing SG (RDS-only egress, used by migration Lambda)
RDS_ONLY_SG="sg-0b224ea9e0b04b7ba"
# Health-check SG (RDS + CloudWatch egress — created by this script)
HEALTH_CHECK_SG_NAME="pipeline-health-check-sg"
# RDS Proxy SG (needs ingress rule for health-check SG)
RDS_PROXY_SG="sg-0c2766ac3105f4f4b"
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/service-role/cardrush_scraper-1755596135935"
LAYER_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:layer:price-scraper-py312:1"
PANDAS_LAYER_ARN="arn:aws:lambda:${REGION}:336392948345:layer:AWSSDKPandas-Python312-Arm64:18"

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

# --- Naming ---
HEALTH_CHECK_FUNCTION="pipeline-health-check"
MIGRATION_FUNCTION="pipeline-migration-runner"
SNS_TOPIC_NAME="cambridge-tcg-pipeline-alerts"
ALARM_PREFIX="pipeline"
CW_NAMESPACE="CambridgeTCG/Pipeline"
ALERT_EMAIL="${ALERT_EMAIL:-}"

echo "============================================================"
echo "Pipeline Monitoring — AWS Deployment"
echo "Region: ${REGION}"
echo "Account: ${ACCOUNT_ID}"
echo "============================================================"

# ----------------------------------------------------------------
# Step 0: Validate
# ----------------------------------------------------------------
echo ""
echo "[0/6] Validating AWS credentials..."
aws sts get-caller-identity --output text > /dev/null
echo "  OK"

# ----------------------------------------------------------------
# Step 1: Add CloudWatch permissions to existing IAM role
# ----------------------------------------------------------------
echo ""
echo "[1/6] Ensuring IAM role has CloudWatch permissions..."

# Check if CloudWatch policy is already attached
if aws iam list-attached-role-policies \
    --role-name cardrush_scraper-1755596135935 \
    --query "AttachedPolicies[?PolicyName=='CloudWatchFullAccess'].PolicyName" \
    --output text 2>/dev/null | grep -q CloudWatch; then
    echo "  CloudWatch policy already attached"
else
    aws iam attach-role-policy \
        --role-name cardrush_scraper-1755596135935 \
        --policy-arn arn:aws:iam::aws:policy/CloudWatchFullAccess
    echo "  Attached CloudWatchFullAccess to role"
fi

# ----------------------------------------------------------------
# Step 2: Create dedicated security group + VPC endpoint
# ----------------------------------------------------------------
echo ""
echo "[2/8] Creating security group and VPC endpoint for health-check..."

# Create or find health-check SG (egress: 5432→RDS proxy + 443→CloudWatch VPC endpoint)
HEALTH_CHECK_SG=$(aws ec2 describe-security-groups \
    --filters "Name=group-name,Values=${HEALTH_CHECK_SG_NAME}" "Name=vpc-id,Values=${VPC_ID}" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null)

if [ "${HEALTH_CHECK_SG}" = "None" ] || [ -z "${HEALTH_CHECK_SG}" ]; then
    HEALTH_CHECK_SG=$(aws ec2 create-security-group \
        --group-name "${HEALTH_CHECK_SG_NAME}" \
        --description "Health-check Lambda: egress to RDS proxy (5432) and CloudWatch VPC endpoint (443)" \
        --vpc-id "${VPC_ID}" \
        --query 'GroupId' --output text)
    echo "  Created SG: ${HEALTH_CHECK_SG}"

    # Revoke default allow-all egress
    aws ec2 revoke-security-group-egress \
        --group-id "${HEALTH_CHECK_SG}" \
        --ip-permissions '[{"IpProtocol":"-1","FromPort":-1,"ToPort":-1,"IpRanges":[{"CidrBlock":"0.0.0.0/0"}]}]' \
        2>/dev/null || true

    # Egress: port 5432 to RDS proxy SG
    aws ec2 authorize-security-group-egress \
        --group-id "${HEALTH_CHECK_SG}" \
        --ip-permissions "[{\"IpProtocol\":\"tcp\",\"FromPort\":5432,\"ToPort\":5432,\"UserIdGroupPairs\":[{\"GroupId\":\"${RDS_PROXY_SG}\"}]}]" \
        2>/dev/null || true

    # Egress: port 443 to 0.0.0.0/0 (CloudWatch VPC endpoint)
    aws ec2 authorize-security-group-egress \
        --group-id "${HEALTH_CHECK_SG}" \
        --ip-permissions '[{"IpProtocol":"tcp","FromPort":443,"ToPort":443,"IpRanges":[{"CidrBlock":"0.0.0.0/0"}]}]' \
        2>/dev/null || true

    # Self-referencing ingress on 443 (needed for VPC endpoint ENIs)
    aws ec2 authorize-security-group-ingress \
        --group-id "${HEALTH_CHECK_SG}" \
        --ip-permissions "[{\"IpProtocol\":\"tcp\",\"FromPort\":443,\"ToPort\":443,\"UserIdGroupPairs\":[{\"GroupId\":\"${HEALTH_CHECK_SG}\"}]}]" \
        2>/dev/null || true

    # Allow health-check SG to reach RDS proxy (add ingress to proxy SG)
    aws ec2 authorize-security-group-ingress \
        --group-id "${RDS_PROXY_SG}" \
        --ip-permissions "[{\"IpProtocol\":\"tcp\",\"FromPort\":5432,\"ToPort\":5432,\"UserIdGroupPairs\":[{\"GroupId\":\"${HEALTH_CHECK_SG}\"}]}]" \
        2>/dev/null || true
    echo "  Configured SG rules (RDS + CloudWatch + self-443)"
else
    echo "  SG already exists: ${HEALTH_CHECK_SG}"
fi

# Create VPC Interface Endpoint for CloudWatch (com.amazonaws.us-east-1.monitoring)
# Lambda subnets don't route through NAT, so a VPC endpoint is required for CloudWatch API
CW_VPCE=$(aws ec2 describe-vpc-endpoints \
    --filters "Name=service-name,Values=com.amazonaws.${REGION}.monitoring" "Name=vpc-id,Values=${VPC_ID}" \
    --query 'VpcEndpoints[0].VpcEndpointId' --output text 2>/dev/null)

if [ "${CW_VPCE}" = "None" ] || [ -z "${CW_VPCE}" ]; then
    CW_VPCE=$(aws ec2 create-vpc-endpoint \
        --vpc-id "${VPC_ID}" \
        --vpc-endpoint-type Interface \
        --service-name "com.amazonaws.${REGION}.monitoring" \
        --subnet-ids ${SUBNET_IDS//,/ } \
        --security-group-ids "${HEALTH_CHECK_SG}" \
        --private-dns-enabled \
        --query 'VpcEndpoint.VpcEndpointId' --output text)
    echo "  Created VPC endpoint: ${CW_VPCE}"
else
    echo "  VPC endpoint already exists: ${CW_VPCE}"
fi

# ----------------------------------------------------------------
# Step 3: Run database migration via temporary Lambda
# ----------------------------------------------------------------
echo ""
echo "[3/8] Running database migration (pipeline_runs table)..."

MIGRATION_SQL=$(cat "${SCRIPT_DIR}/migrations/002_add_pipeline_runs.sql")

# Create inline migration Lambda code
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
        # Verify table exists
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM pipeline_runs")
            count = cursor.fetchone()[0]
        return {'statusCode': 200, 'body': f'Migration complete. pipeline_runs has {count} rows.'}
    except Exception as e:
        connection.rollback()
        return {'statusCode': 500, 'body': str(e)}
    finally:
        connection.close()
PYEOF

# Package it
(cd "${MIGRATION_DIR}" && zip -q migration.zip lambda_function.py)

# Create temporary Lambda (uses RDS-only SG since it only needs DB access)
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

# Wait for Lambda to be active
echo "  Waiting for Lambda to be active..."
aws lambda wait function-active-v2 --function-name "${MIGRATION_FUNCTION}"

# Invoke migration
echo "  Running migration SQL..."
MIGRATION_PAYLOAD=$(printf '{"sql": %s}' "$(echo "${MIGRATION_SQL}" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))')")

MIGRATION_RESULT=$(aws lambda invoke \
    --function-name "${MIGRATION_FUNCTION}" \
    --payload "${MIGRATION_PAYLOAD}" \
    --cli-binary-format raw-in-base64-out \
    /tmp/migration_result.json \
    --output text --query 'StatusCode' 2>&1)

MIGRATION_BODY=$(cat /tmp/migration_result.json)
echo "  Result: ${MIGRATION_BODY}"

# Clean up temporary Lambda
echo "  Cleaning up migration Lambda..."
aws lambda delete-function --function-name "${MIGRATION_FUNCTION}" 2>/dev/null || true
rm -rf "${MIGRATION_DIR}"

# ----------------------------------------------------------------
# Step 4: Deploy health-check Lambda
# ----------------------------------------------------------------
echo ""
echo "[4/8] Deploying health-check Lambda..."

DEPLOY_DIR=$(mktemp -d)
# Package monitoring directory
cp "${SCRIPT_DIR}/monitoring/lambda_function.py" "${DEPLOY_DIR}/"
cp "${SCRIPT_DIR}/monitoring/metrics.py" "${DEPLOY_DIR}/"
(cd "${DEPLOY_DIR}" && zip -q health-check.zip lambda_function.py metrics.py)

if aws lambda get-function --function-name "${HEALTH_CHECK_FUNCTION}" > /dev/null 2>&1; then
    aws lambda update-function-code \
        --function-name "${HEALTH_CHECK_FUNCTION}" \
        --zip-file "fileb://${DEPLOY_DIR}/health-check.zip" \
        --output text > /dev/null
    echo "  Updated existing health-check Lambda code"

    # Wait for update to complete before updating config
    aws lambda wait function-updated-v2 --function-name "${HEALTH_CHECK_FUNCTION}"

    aws lambda update-function-configuration \
        --function-name "${HEALTH_CHECK_FUNCTION}" \
        --timeout 120 \
        --memory-size 256 \
        --layers "${LAYER_ARN}" \
        --vpc-config "SubnetIds=${SUBNET_IDS},SecurityGroupIds=${HEALTH_CHECK_SG}" \
        --environment "Variables={PROXY_ENDPOINT=${PROXY_ENDPOINT},DB_USER=${DB_USER},DB_PASSWORD=${DB_PASSWORD},DATABASE_NAME=${DB_NAME},TABLE_NAME=${TABLE_NAME},DB_PORT=5432,STALENESS_HOURS=26}" \
        --output text > /dev/null
    echo "  Updated health-check Lambda config"
else
    aws lambda create-function \
        --function-name "${HEALTH_CHECK_FUNCTION}" \
        --runtime python3.12 \
        --handler lambda_function.lambda_handler \
        --role "${ROLE_ARN}" \
        --zip-file "fileb://${DEPLOY_DIR}/health-check.zip" \
        --timeout 120 \
        --memory-size 256 \
        --architectures arm64 \
        --layers "${LAYER_ARN}" \
        --vpc-config "SubnetIds=${SUBNET_IDS},SecurityGroupIds=${HEALTH_CHECK_SG}" \
        --environment "Variables={PROXY_ENDPOINT=${PROXY_ENDPOINT},DB_USER=${DB_USER},DB_PASSWORD=${DB_PASSWORD},DATABASE_NAME=${DB_NAME},TABLE_NAME=${TABLE_NAME},DB_PORT=5432,STALENESS_HOURS=26}" \
        --output text > /dev/null
    echo "  Created health-check Lambda"
fi

aws lambda wait function-active-v2 --function-name "${HEALTH_CHECK_FUNCTION}"
echo "  Lambda is active"

rm -rf "${DEPLOY_DIR}"

# ----------------------------------------------------------------
# Step 5: Create EventBridge schedule (every 30 min)
# ----------------------------------------------------------------
echo ""
echo "[5/8] Creating EventBridge schedule..."

RULE_NAME="pipeline-health-check-schedule"
HEALTH_CHECK_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${HEALTH_CHECK_FUNCTION}"

aws events put-rule \
    --name "${RULE_NAME}" \
    --schedule-expression "rate(6 hours)" \
    --state ENABLED \
    --description "Trigger pipeline health check every 6 hours" \
    --output text > /dev/null
echo "  Created/updated EventBridge rule: ${RULE_NAME}"

# Add Lambda permission for EventBridge
aws lambda add-permission \
    --function-name "${HEALTH_CHECK_FUNCTION}" \
    --statement-id "EventBridgeInvoke" \
    --action "lambda:InvokeFunction" \
    --principal "events.amazonaws.com" \
    --source-arn "arn:aws:events:${REGION}:${ACCOUNT_ID}:rule/${RULE_NAME}" \
    2>/dev/null || echo "  Permission already exists"

aws events put-targets \
    --rule "${RULE_NAME}" \
    --targets "Id=health-check-target,Arn=${HEALTH_CHECK_ARN}" \
    --output text > /dev/null
echo "  Linked rule to Lambda"

# ----------------------------------------------------------------
# Step 6: Create SNS topic + CloudWatch alarms
# ----------------------------------------------------------------
echo ""
echo "[6/8] Creating SNS topic and CloudWatch alarms..."

# Create SNS topic
SNS_TOPIC_ARN=$(aws sns create-topic \
    --name "${SNS_TOPIC_NAME}" \
    --query 'TopicArn' --output text)
echo "  SNS topic: ${SNS_TOPIC_ARN}"

# Subscribe email if provided
if [ -n "${ALERT_EMAIL}" ]; then
    aws sns subscribe \
        --topic-arn "${SNS_TOPIC_ARN}" \
        --protocol email \
        --notification-endpoint "${ALERT_EMAIL}" \
        --output text > /dev/null
    echo "  Subscribed ${ALERT_EMAIL} (check inbox to confirm)"
else
    echo "  No ALERT_EMAIL set — subscribe manually:"
    echo "    aws sns subscribe --topic-arn ${SNS_TOPIC_ARN} --protocol email --notification-endpoint your@email.com"
fi

# Create CloudWatch alarms
# Staleness alarms (value in seconds, threshold = 26h = 93600s)
for STAGE in Scraper Fx Calculator Shopify Ebay; do
    STAGE_LOWER=$(echo "${STAGE}" | tr '[:upper:]' '[:lower:]')
    ALARM_NAME="${ALARM_PREFIX}-${STAGE_LOWER}-stale"
    METRIC_NAME="${STAGE}Staleness"

    aws cloudwatch put-metric-alarm \
        --alarm-name "${ALARM_NAME}" \
        --alarm-description "${STAGE} pipeline stage has not run in over 26 hours" \
        --namespace "${CW_NAMESPACE}" \
        --metric-name "${METRIC_NAME}" \
        --statistic Maximum \
        --period 1800 \
        --evaluation-periods 1 \
        --threshold 93600 \
        --comparison-operator GreaterThanThreshold \
        --alarm-actions "${SNS_TOPIC_ARN}" \
        --treat-missing-data notBreaching \
        --output text > /dev/null

    echo "  Alarm: ${ALARM_NAME} (${METRIC_NAME} > 93600s)"
done

# Zero-row update alarm
aws cloudwatch put-metric-alarm \
    --alarm-name "${ALARM_PREFIX}-zero-rows" \
    --alarm-description "Scraper produced zero rows in last run" \
    --namespace "${CW_NAMESPACE}" \
    --metric-name "ZeroRowUpdate" \
    --statistic Maximum \
    --period 1800 \
    --evaluation-periods 1 \
    --threshold 0.5 \
    --comparison-operator GreaterThanOrEqualToThreshold \
    --alarm-actions "${SNS_TOPIC_ARN}" \
    --treat-missing-data notBreaching \
    --output text > /dev/null
echo "  Alarm: ${ALARM_PREFIX}-zero-rows (ZeroRowUpdate >= 1)"

# Price anomalies alarm
aws cloudwatch put-metric-alarm \
    --alarm-name "${ALARM_PREFIX}-price-anomalies" \
    --alarm-description "Products with prices outside expected 1.80-500 range" \
    --namespace "${CW_NAMESPACE}" \
    --metric-name "PriceAnomalies" \
    --statistic Maximum \
    --period 1800 \
    --evaluation-periods 1 \
    --threshold 0.5 \
    --comparison-operator GreaterThanOrEqualToThreshold \
    --alarm-actions "${SNS_TOPIC_ARN}" \
    --treat-missing-data notBreaching \
    --output text > /dev/null
echo "  Alarm: ${ALARM_PREFIX}-price-anomalies (PriceAnomalies >= 1)"

# Health-check Lambda errors alarm
aws cloudwatch put-metric-alarm \
    --alarm-name "${ALARM_PREFIX}-health-check-errors" \
    --alarm-description "Health check Lambda itself is failing" \
    --namespace "AWS/Lambda" \
    --metric-name "Errors" \
    --dimensions "Name=FunctionName,Value=${HEALTH_CHECK_FUNCTION}" \
    --statistic Sum \
    --period 1800 \
    --evaluation-periods 1 \
    --threshold 0.5 \
    --comparison-operator GreaterThanOrEqualToThreshold \
    --alarm-actions "${SNS_TOPIC_ARN}" \
    --treat-missing-data notBreaching \
    --output text > /dev/null
echo "  Alarm: ${ALARM_PREFIX}-health-check-errors (Lambda Errors >= 1)"

# ----------------------------------------------------------------
# Step 7: Create monitoring Lambda layer for existing Lambdas
# ----------------------------------------------------------------
echo ""
echo "[7/8] Creating monitoring layer for existing Lambdas..."

LAYER_DIR=$(mktemp -d)
mkdir -p "${LAYER_DIR}/python/monitoring"
cp "${SCRIPT_DIR}/monitoring/__init__.py" "${LAYER_DIR}/python/monitoring/"
cp "${SCRIPT_DIR}/monitoring/metrics.py" "${LAYER_DIR}/python/monitoring/"
(cd "${LAYER_DIR}" && zip -qr monitoring-layer.zip python/)

MONITORING_LAYER_ARN=$(aws lambda publish-layer-version \
    --layer-name "pipeline-monitoring" \
    --description "Pipeline monitoring helpers (record_pipeline_run, put_metric, put_metrics_batch)" \
    --zip-file "fileb://${LAYER_DIR}/monitoring-layer.zip" \
    --compatible-runtimes python3.12 \
    --compatible-architectures arm64 \
    --query 'LayerVersionArn' --output text)
echo "  Published layer: ${MONITORING_LAYER_ARN}"

# Update existing Lambdas to include the monitoring layer
# Build layer list: keep existing non-monitoring layers, append new monitoring layer
for FUNC_NAME in cardrush_scraper price_calculator; do
    # Get current layers as newline-separated list
    CURRENT_LAYERS=$(aws lambda get-function-configuration \
        --function-name "${FUNC_NAME}" \
        --query 'Layers[].Arn' --output text | tr '\t' '\n')

    # Filter out any existing monitoring layer version, add new one
    LAYER_ARGS=()
    while IFS= read -r arn; do
        [ -z "${arn}" ] && continue
        echo "${arn}" | grep -q "pipeline-monitoring" && continue
        LAYER_ARGS+=("${arn}")
    done <<< "${CURRENT_LAYERS}"
    LAYER_ARGS+=("${MONITORING_LAYER_ARN}")

    aws lambda update-function-configuration \
        --function-name "${FUNC_NAME}" \
        --layers "${LAYER_ARGS[@]}" \
        --output text > /dev/null
    echo "  ${FUNC_NAME}: layers updated"
done

rm -rf "${LAYER_DIR}"

# ----------------------------------------------------------------
# Step 8: Smoke test — invoke health check
# ----------------------------------------------------------------
echo ""
echo "[8/8] Running smoke test..."

aws lambda wait function-active-v2 --function-name "${HEALTH_CHECK_FUNCTION}" 2>/dev/null || true

INVOKE_STATUS=$(aws lambda invoke \
    --function-name "${HEALTH_CHECK_FUNCTION}" \
    /tmp/hc-result.json \
    --query 'StatusCode' --output text 2>&1)

if [ "${INVOKE_STATUS}" = "200" ]; then
    HC_BODY=$(python3 -c "import json; d=json.load(open('/tmp/hc-result.json')); b=json.loads(d['body']); print(f\"  {b['checks_passed']}/{b['total_checks']} checks passed, {b['checks_failed']} failed\")" 2>/dev/null || echo "  Invoked OK (could not parse body)")
    echo "${HC_BODY}"
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
echo "  - SG: ${HEALTH_CHECK_SG} (RDS + CloudWatch egress)"
echo "  - VPC Endpoint: ${CW_VPCE} (com.amazonaws.${REGION}.monitoring)"
echo "  - Table: pipeline_runs (in ${DB_NAME})"
echo "  - Lambda: ${HEALTH_CHECK_FUNCTION} (120s timeout, 256MB)"
echo "  - EventBridge: ${RULE_NAME} (every 30 min)"
echo "  - SNS: ${SNS_TOPIC_ARN}"
echo "  - Alarms: 8 CloudWatch alarms"
echo "  - Layer: ${MONITORING_LAYER_ARN}"
echo ""
echo "To test the health check manually:"
echo "  aws lambda invoke --function-name ${HEALTH_CHECK_FUNCTION} /tmp/hc-result.json && cat /tmp/hc-result.json | python3 -m json.tool"
echo "============================================================"
