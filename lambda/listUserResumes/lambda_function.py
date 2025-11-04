import json
import boto3
import os
from auth import get_user_id_from_event, create_unauthorized_response, CORS_HEADERS

dynamodb = boto3.resource('dynamodb')
TABLE_NAME = os.environ.get('TABLE_NAME')
table = dynamodb.Table(TABLE_NAME)

def lambda_handler(event, context):
    """
    Lists all resumes uploaded by the authenticated user.
    Returns a list of fileId, filename, upload date, and processing status.
    """
    # ===== AUTHENTICATION CHECK =====
    user_id = get_user_id_from_event(event)
    if not user_id:
        print("Authentication failed - no valid user_id")
        return create_unauthorized_response("Authentication required")

    print(f"Authenticated user: {user_id}")

    try:
        # Scan the table for all items belonging to this user
        # Note: In production with large datasets, consider using a GSI (Global Secondary Index) on userId
        response = table.scan(
            FilterExpression='userId = :uid',
            ExpressionAttributeValues={
                ':uid': user_id
            }
        )

        items = response.get('Items', [])

        # Format the response to only include necessary fields
        resumes = []
        for item in items:
            resume = {
                'fileId': item.get('fileId'),
                'originalFilename': item.get('originalFilename'),
                'processingStatus': item.get('processingStatus'),
                'uploadedAt': item.get('fileId')  # fileId contains timestamp in UUID
            }
            resumes.append(resume)

        # Sort by fileId (most recent first - assuming UUID v4 with timestamp)
        resumes.sort(key=lambda x: x.get('fileId', ''), reverse=True)

        print(f"Found {len(resumes)} resumes for user {user_id}")

        return {
            "statusCode": 200,
            "headers": CORS_HEADERS,
            "body": json.dumps({
                "resumes": resumes,
                "count": len(resumes)
            })
        }

    except Exception as e:
        print(f"Error listing resumes: {e}")
        return {
            "statusCode": 500,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": "Failed to list resumes"})
        }
