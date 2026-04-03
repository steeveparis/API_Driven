#!/usr/bin/env bash
set -e

source .env.generated

echo "[TEST] URL START"
curl -s "$START_URL"
echo
echo

echo "[TEST] État instance après start"
aws --endpoint-url="$AWS_ENDPOINT" --region us-east-1 ec2 describe-instances \
  --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].State.Name' \
  --output text

echo
echo "[TEST] URL STOP"
curl -s "$STOP_URL"
echo
echo

echo "[TEST] État instance après stop"
aws --endpoint-url="$AWS_ENDPOINT" --region us-east-1 ec2 describe-instances \
  --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].State.Name' \
  --output text
