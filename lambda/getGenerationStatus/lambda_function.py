import json
import os
import boto3
from decimal import Decimal

# Initialize DynamoDB client
dynamodb = boto3.resource('dynamodb')

# Environment variables
GENERATION_JOBS_TABLE = os.environ.get('GENERATION_JOBS_TABLE')

# CORS headers
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "OPTIONS,POST,GET"
}

# Helper function to convert Decimal to int/float for JSON serialization
def convert_decimal(obj):
    """Convert DynamoDB Decimal objects to int or float for JSON serialization."""
    if isinstance(obj, Decimal):
        if obj % 1 == 0:
            return int(obj)
        else:
            return float(obj)
    elif isinstance(obj, dict):
        return {k: convert_decimal(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_decimal(i) for i in obj]
    return obj

def lambda_handler(event, context):
    """
    Returns the status and results of a generation job.
    Used by the frontend for polling.
    """
    try:
        # Get jobId from query parameters
        params = event.get('queryStringParameters', {}) or {}
        job_id = params.get('jobId')

        if not job_id:
            return {
                "statusCode": 400,
                "headers": CORS_HEADERS,
                "body": json.dumps({"error": "jobId query parameter is required"})
            }

        # Query DynamoDB
        table = dynamodb.Table(GENERATION_JOBS_TABLE)
        response = table.get_item(Key={'jobId': job_id})

        if 'Item' not in response:
            return {
                "statusCode": 404,
                "headers": CORS_HEADERS,
                "body": json.dumps({"error": "Job not found"})
            }

        item = response['Item']

        # Convert Decimal objects to int/float
        item = convert_decimal(item)

        # Build response based on status
        result = {
            "jobId": item['jobId'],
            "status": item['status'],
            "createdAt": item.get('createdAt')
        }

        if item['status'] == 'COMPLETED':
            result['tailoredResume'] = item.get('tailoredResume', '')
            result['coverLetter'] = item.get('coverLetter', '')
            result['completedAt'] = item.get('completedAt')

        elif item['status'] == 'FAILED':
            result['errorMessage'] = item.get('errorMessage', 'Unknown error')

        return {
            "statusCode": 200,
            "headers": CORS_HEADERS,
            "body": json.dumps(result)
        }

    except Exception as e:
        print(f"Error getting generation status: {e}")
        return {
            "statusCode": 500,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": f"Failed to get status: {str(e)}"})
        }
