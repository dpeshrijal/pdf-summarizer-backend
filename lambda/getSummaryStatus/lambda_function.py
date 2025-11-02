import json
import boto3
import os
import sys

# Add auth module to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from auth import get_user_id_from_event, create_unauthorized_response, create_forbidden_response, CORS_HEADERS

dynamodb = boto3.resource('dynamodb')
TABLE_NAME = os.environ.get('TABLE_NAME')
table = dynamodb.Table(TABLE_NAME)

def lambda_handler(event, context):
    # ===== AUTHENTICATION CHECK =====
    user_id = get_user_id_from_event(event)
    if not user_id:
        print("Authentication failed - no valid user_id")
        return create_unauthorized_response("Authentication required")

    print(f"Authenticated user: {user_id}")

    try:
        query_params = event.get('queryStringParameters', {})
        file_id = query_params.get('fileId')

        if not file_id:
            return {
                "statusCode": 400,
                "headers": CORS_HEADERS,
                "body": json.dumps({"error": "fileId is required"})
            }

        response = table.get_item(Key={'fileId': file_id})
        item = response.get('Item', {})

        # ===== AUTHORIZATION CHECK =====
        # Verify the file belongs to the authenticated user
        if item:
            file_owner = item.get('userId')
            if file_owner and file_owner != user_id:
                print(f"User {user_id} tried to access file {file_id} owned by {file_owner}")
                return create_forbidden_response("You don't have permission to access this file")

        return {
            "statusCode": 200,
            "headers": CORS_HEADERS,
            "body": json.dumps(item)
        }
    except Exception as e:
        print(f"Error: {e}")
        return {
            "statusCode": 500,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": "Failed to retrieve status"})
        }