import json
import boto3
import os

dynamodb = boto3.resource('dynamodb')
s3_client = boto3.client('s3')

TABLE_NAME = os.environ.get('TABLE_NAME')
BUCKET_NAME = os.environ.get('BUCKET_NAME')
table = dynamodb.Table(TABLE_NAME)

CORS_HEADERS = { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "Content-Type" }

def lambda_handler(event, context):
    try:
        query_params = event.get('queryStringParameters', {})
        file_id = query_params.get('fileId')

        if not file_id:
            return { "statusCode": 400, "headers": CORS_HEADERS, "body": json.dumps({"error": "fileId is required"}) }

        response = table.get_item(Key={'fileId': file_id})
        item = response.get('Item', {})
        
        # --- NEW LOGIC: GENERATE DOWNLOAD URL IF COMPLETE ---
        if item.get('processingStatus') == 'COMPLETED':
            s3_key = item.get('generatedPdfKey')
            if s3_key:
                print(f"Generating download URL for s3://{BUCKET_NAME}/{s3_key}")
                download_url = s3_client.generate_presigned_url(
                    'get_object',
                    Params={'Bucket': BUCKET_NAME, 'Key': s3_key},
                    ExpiresIn=300 # Link is valid for 5 minutes
                )
                item['downloadUrl'] = download_url

        return { "statusCode": 200, "headers": CORS_HEADERS, "body": json.dumps(item) }

    except Exception as e:
        print(f"Error: {e}")
        return { "statusCode": 500, "headers": CORS_HEADERS, "body": json.dumps({"error": "Failed to retrieve status"}) }