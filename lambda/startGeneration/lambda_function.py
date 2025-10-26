import json
import os
import boto3
import uuid
import time

# Initialize clients
dynamodb = boto3.resource('dynamodb')
lambda_client = boto3.client('lambda')

# Environment variables
GENERATION_JOBS_TABLE = os.environ.get('GENERATION_JOBS_TABLE')
PROCESS_GENERATION_FUNCTION_NAME = os.environ.get('PROCESS_GENERATION_FUNCTION_NAME')

# CORS headers
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "OPTIONS,POST,GET"
}

def lambda_handler(event, context):
    """
    Handles the initial request to generate documents.
    Creates a job in DynamoDB and invokes the processing Lambda asynchronously.
    Returns immediately with a jobId for the frontend to poll.
    """
    try:
        # Parse request body
        body = json.loads(event.get('body', '{}'))

        file_id = body.get('fileId')
        job_description = body.get('jobDescription')

        if not file_id or not job_description:
            return {
                "statusCode": 400,
                "headers": CORS_HEADERS,
                "body": json.dumps({"error": "fileId and jobDescription are required"})
            }

        # Generate unique job ID
        job_id = str(uuid.uuid4())

        # Calculate TTL (24 hours from now)
        ttl = int(time.time()) + 86400

        # Create job entry in DynamoDB
        table = dynamodb.Table(GENERATION_JOBS_TABLE)
        table.put_item(Item={
            'jobId': job_id,
            'fileId': file_id,
            'jobDescription': job_description,
            'status': 'PROCESSING',
            'createdAt': int(time.time()),
            'ttl': ttl
        })

        print(f"Created generation job: {job_id}")

        # Invoke processing Lambda asynchronously
        lambda_client.invoke(
            FunctionName=PROCESS_GENERATION_FUNCTION_NAME,
            InvocationType='Event',  # Async invocation
            Payload=json.dumps({
                'jobId': job_id,
                'fileId': file_id,
                'jobDescription': job_description
            })
        )

        print(f"Invoked processing Lambda for job: {job_id}")

        # Return jobId immediately
        return {
            "statusCode": 200,
            "headers": CORS_HEADERS,
            "body": json.dumps({
                "jobId": job_id,
                "status": "PROCESSING",
                "message": "Document generation started. Use jobId to poll for status."
            })
        }

    except Exception as e:
        print(f"Error starting generation: {e}")
        return {
            "statusCode": 500,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": f"Failed to start generation: {str(e)}"})
        }
