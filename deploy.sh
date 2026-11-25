#!/usr/bin/env bash
# Deploy the COP361 GPU fleet dashboard to a CloudWatch Omni Space with CloudFormation.
#
#   SPACE_ID=<omni-space-id> CLUSTER_NAME=<eks-cluster> ARTIFACT_BUCKET=<s3-bucket> ./deploy.sh
#
# Optional: STORY=1 (deploy the story version, template-story.yaml), AWS_REGION (default: your CLI's
# configured Region), STACK_NAME (default cop361-gpu-dashboard), DASHBOARD_NAME (default cop361-gpu-fleet).
# The template is larger than CloudFormation's 51,200-byte inline limit, so `deploy` uploads it to
# ARTIFACT_BUCKET first. Use a bucket you own in the same Region.
set -euo pipefail

: "${SPACE_ID:?set SPACE_ID to your CloudWatch Omni Space ID (see README.md)}"
: "${CLUSTER_NAME:?set CLUSTER_NAME to your EKS cluster name}"
: "${ARTIFACT_BUCKET:?set ARTIFACT_BUCKET to an S3 bucket you own in the target Region}"
STACK_NAME="${STACK_NAME:-cop361-gpu-dashboard}"
HERE="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE="$HERE/template.yaml"
[ "${STORY:-}" = "1" ] && TEMPLATE="$HERE/template-story.yaml"

params=("SpaceId=$SPACE_ID" "ClusterName=$CLUSTER_NAME")
[ -n "${DASHBOARD_NAME:-}" ] && params+=("DashboardName=$DASHBOARD_NAME")

aws cloudformation deploy \
  --stack-name "$STACK_NAME" \
  --template-file "$TEMPLATE" \
  --s3-bucket "$ARTIFACT_BUCKET" \
  --s3-prefix "$STACK_NAME" \
  --parameter-overrides "${params[@]}" \
  --no-fail-on-empty-changeset

aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output table
