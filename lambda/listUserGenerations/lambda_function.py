import json
import boto3
import os
from decimal import Decimal
from auth import get_user_id_from_event, create_unauthorized_response, CORS_HEADERS

dynamodb = boto3.resource('dynamodb')
GENERATION_JOBS_TABLE = os.environ.get('GENERATION_JOBS_TABLE')
table = dynamodb.Table(GENERATION_JOBS_TABLE)

def decimal_to_int(obj):
    """Convert Decimal types to int for JSON serialization"""
    if isinstance(obj, Decimal):
        return int(obj)
    return obj

def lambda_handler(event, context):
    """
    Lists all completed generation jobs for the authenticated user.
    Returns company name, date, resume, and cover letter.
    """
    # ===== AUTHENTICATION CHECK =====
    user_id = get_user_id_from_event(event)
    if not user_id:
        print("Authentication failed - no valid user_id")
        return create_unauthorized_response("Authentication required")

    print(f"Authenticated user: {user_id}")

    try:
        # Scan the generation jobs table for completed jobs belonging to this user
        response = table.scan(
            FilterExpression='userId = :uid AND #status = :status',
            ExpressionAttributeNames={
                '#status': 'status'
            },
            ExpressionAttributeValues={
                ':uid': user_id,
                ':status': 'COMPLETED'
            }
        )

        items = response.get('Items', [])

        # Format the response
        generations = []
        for item in items:
            generation = {
                'jobId': item.get('jobId'),
                'companyName': item.get('companyName', 'Unknown Company'),
                'jobTitle': item.get('jobTitle', 'Unknown Position'),
                'completedAt': decimal_to_int(item.get('completedAt')),
                'createdAt': decimal_to_int(item.get('createdAt')),
                'fileId': item.get('fileId')
            }
            # Add structured data if available (new format)
            if 'structuredData' in item:
                generation['structuredData'] = item.get('structuredData')
            # Add old format fields for backward compatibility
            if 'tailoredResume' in item:
                generation['tailoredResume'] = item.get('tailoredResume')
            if 'coverLetter' in item:
                generation['coverLetter'] = item.get('coverLetter')
            generations.append(generation)

        # Sort by completedAt (most recent first)
        generations.sort(key=lambda x: x.get('completedAt', 0), reverse=True)

        print(f"Found {len(generations)} completed generations for user {user_id}")

        return {
            "statusCode": 200,
            "headers": CORS_HEADERS,
            "body": json.dumps({
                "generations": generations,
                "count": len(generations)
            })
        }

    except Exception as e:
        print(f"Error listing generations: {e}")
        return {
            "statusCode": 500,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": "Failed to list generations"})
        }
