import json
import os
import boto3

AWS_ENDPOINT = os.environ.get("AWS_ENDPOINT", "https://super-duper-space-umbrella-9v75rggg64r2pqjx-4566.app.github.dev/")
INSTANCE_ID = os.environ.get("INSTANCE_ID")

ec2 = boto3.client("ec2", endpoint_url=AWS_ENDPOINT, region_name="us-east-1")

def lambda_handler(event, context):
    path = event.get("path", "")
    method = event.get("httpMethod", "")

    if method != "POST":
        return {
            "statusCode": 405,
            "body": json.dumps({"error": "Méthode non autorisée"})
        }

    if path.endswith("/start"):
        result = ec2.start_instances(InstanceIds=[INSTANCE_ID])
        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Instance démarrée",
                "instance_id": INSTANCE_ID,
                "result": result
            })
        }

    if path.endswith("/stop"):
        result = ec2.stop_instances(InstanceIds=[INSTANCE_ID])
        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Instance arrêtée",
                "instance_id": INSTANCE_ID,
                "result": result
            })
        }

    return {
        "statusCode": 404,
        "body": json.dumps({"error": "Route inconnue"})
    }
