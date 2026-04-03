import json
import os
import boto3

AWS_ENDPOINT = os.environ.get("AWS_ENDPOINT", "http://localhost:4566")
INSTANCE_ID = os.environ.get("INSTANCE_ID")

ec2 = boto3.client("ec2", endpoint_url=AWS_ENDPOINT, region_name="us-east-1")

def lambda_handler(event, context):
    path = event.get("rawPath") or event.get("path", "")
    http_method = event.get("requestContext", {}).get("http", {}).get("method") or event.get("httpMethod", "")

    if not INSTANCE_ID:
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "INSTANCE_ID manquant"})
        }

    if http_method not in ["GET", "POST"]:
        return {
            "statusCode": 405,
            "body": json.dumps({"error": "Méthode non autorisée"})
        }

    try:
        if path.endswith("/start"):
            response = ec2.start_instances(InstanceIds=[INSTANCE_ID])
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "message": "Instance démarrée",
                    "instance_id": INSTANCE_ID,
                    "details": response
                })
            }

        if path.endswith("/stop"):
            response = ec2.stop_instances(InstanceIds=[INSTANCE_ID])
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "message": "Instance arrêtée",
                    "instance_id": INSTANCE_ID,
                    "details": response
                })
            }

        return {
            "statusCode": 404,
            "body": json.dumps({"error": "Route inconnue"})
        }

    except Exception as e:
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)})
        }
