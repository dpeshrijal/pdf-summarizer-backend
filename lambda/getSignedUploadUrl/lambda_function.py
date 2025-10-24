import json
import boto3
import uuid
import os

s3_client = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')

# CDK will pass the real bucket and table names via environment variables
BUCKET_NAME = os.environ.get('BUCKET_NAME')
TABLE_NAME = os.environ.get('TABLE_NAME')

def lambda_handler(event, context):
    try:
        query_params = event.get('queryStringParameters', {})
        original_filename = query_params.get('fileName', 'unknown.pdf')

        file_id = str(uuid.uuid4())
        # We prefix with the file_id to ensure filename uniqueness in S3
        s3_key = f"{file_id}-{original_filename}"

        table = dynamodb.Table(TABLE_NAME)
        table.put_item(
            Item={
                'fileId': file_id,
                'originalFilename': original_filename,
                'processingStatus': 'PENDING'
            }
        )

        presigned_url = s3_client.generate_presigned_url(
            'put_object',
            Params={
                'Bucket': BUCKET_NAME,
                'Key': s3_key,
                'ContentType': 'application/pdf',
                'Metadata': {
                    'fileid': file_id
                }
            },
            ExpiresIn=3600  # URL is valid for 1 hour
        )

        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*" # API Gateway will handle CORS more robustly
            },
            "body": json.dumps({
                "uploadUrl": presigned_url,
                "fileId": file_id,
                "s3Key": s3_key
            })
        }
    except Exception as e:
        print(f"Error: {e}")
        return {
            "statusCode": 500,
            "headers": { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" },
            "body": json.dumps({"error": "Failed to generate URL"})
        }