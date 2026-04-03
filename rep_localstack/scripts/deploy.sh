#!/usr/bin/env bash
set -e

AWS_ENDPOINT="${AWS_ENDPOINT:-http://localhost:4566}"
REGION="us-east-1"
FUNCTION_NAME="ec2-control"
ROLE_ARN="arn:aws:iam::000000000000:role/lambda-role"

echo "AWS_ENDPOINT=$AWS_ENDPOINT"

echo "[1/8] Création instance EC2 simulée"
INSTANCE_ID=$(aws --endpoint-url="$AWS_ENDPOINT" --region "$REGION" ec2 run-instances \
  --image-id ami-12345678 \
  --count 1 \
  --instance-type t2.micro \
  --query 'Instances[0].InstanceId' \
  --output text)

echo "INSTANCE_ID=$INSTANCE_ID"

echo "[2/8] Packaging Lambda"
cd lambda
zip -r ../lambda.zip ec2_control.py >/dev/null
cd ..

echo "[3/8] Création fonction Lambda"
aws --endpoint-url="$AWS_ENDPOINT" --region "$REGION" lambda create-function \
  --function-name "$FUNCTION_NAME" \
  --runtime python3.11 \
  --role "$ROLE_ARN" \
  --handler ec2_control.lambda_handler \
  --zip-file fileb://lambda.zip \
  --environment "Variables={AWS_ENDPOINT=$AWS_ENDPOINT,INSTANCE_ID=$INSTANCE_ID}" \
  >/dev/null

echo "[4/8] Création API Gateway"
API_ID=$(aws --endpoint-url="$AWS_ENDPOINT" --region "$REGION" apigatewayv2 create-api \
  --name ec2-api \
  --protocol-type HTTP \
  --query 'ApiId' \
  --output text)

echo "API_ID=$API_ID"

echo "[5/8] Création intégration Lambda"
INTEGRATION_ID=$(aws --endpoint-url="$AWS_ENDPOINT" --region "$REGION" apigatewayv2 create-integration \
  --api-id "$API_ID" \
  --integration-type AWS_PROXY \
  --integration-uri "arn:aws:lambda:${REGION}:000000000000:function:${FUNCTION_NAME}" \
  --payload-format-version "2.0" \
  --query 'IntegrationId' \
  --output text)

echo "INTEGRATION_ID=$INTEGRATION_ID"

echo "[6/8] Création routes /start et /stop"
aws --endpoint-url="$AWS_ENDPOINT" --region "$REGION" apigatewayv2 create-route \
  --api-id "$API_ID" \
  --route-key "GET /start" \
  --target "integrations/$INTEGRATION_ID" >/dev/null

aws --endpoint-url="$AWS_ENDPOINT" --region "$REGION" apigatewayv2 create-route \
  --api-id "$API_ID" \
  --route-key "GET /stop" \
  --target "integrations/$INTEGRATION_ID" >/dev/null

echo "[7/8] Déploiement stage"
aws --endpoint-url="$AWS_ENDPOINT" --region "$REGION" apigatewayv2 create-stage \
  --api-id "$API_ID" \
  --stage-name dev \
  --auto-deploy >/dev/null

echo "[8/8] Sauvegarde variables"
cat > .env.generated <<EOF
AWS_ENDPOINT=$AWS_ENDPOINT
INSTANCE_ID=$INSTANCE_ID
API_ID=$API_ID
START_URL=$AWS_ENDPOINT/restapis/$API_ID/dev/_user_request_/start
STOP_URL=$AWS_ENDPOINT/restapis/$API_ID/dev/_user_request_/stop
EOF

echo
echo "Déploiement terminé."
cat .env.generated
