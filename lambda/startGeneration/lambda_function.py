import json
import os
import boto3
import uuid
import time
from auth import get_user_id_from_event, create_unauthorized_response, create_forbidden_response, CORS_HEADERS

# Initialize clients
dynamodb = boto3.resource('dynamodb')
lambda_client = boto3.client('lambda')

# Environment variables
GENERATION_JOBS_TABLE = os.environ.get('GENERATION_JOBS_TABLE')
PROCESS_GENERATION_FUNCTION_NAME = os.environ.get('PROCESS_GENERATION_FUNCTION_NAME')
SUMMARIES_TABLE = os.environ.get('SUMMARIES_TABLE')  # To verify user owns the file
USER_PROFILES_TABLE = os.environ.get('USER_PROFILES_TABLE')  # To check user credits

def lambda_handler(event, context):
    """
    Handles the initial request to generate documents.
    Creates a job in DynamoDB and invokes the processing Lambda asynchronously.
    Returns immediately with a jobId for the frontend to poll.
    """
    # ===== AUTHENTICATION CHECK =====
    user_id = get_user_id_from_event(event)
    if not user_id:
        print("Authentication failed - no valid user_id")
        return create_unauthorized_response("Authentication required")

    print(f"Authenticated user: {user_id}")

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

        # ===== AUTHORIZATION CHECK =====
        # Verify user owns the file they're trying to generate documents for
        summaries_table = dynamodb.Table(SUMMARIES_TABLE)
        try:
            file_response = summaries_table.get_item(Key={'fileId': file_id})
            if 'Item' not in file_response:
                print(f"File not found: {file_id}")
                return {
                    "statusCode": 404,
                    "headers": CORS_HEADERS,
                    "body": json.dumps({"error": "File not found"})
                }

            # Check if the file belongs to the authenticated user
            file_owner = file_response['Item'].get('userId')
            if file_owner and file_owner != user_id:
                print(f"User {user_id} tried to access file {file_id} owned by {file_owner}")
                return create_forbidden_response("You don't have permission to access this file")
        except Exception as e:
            print(f"Error checking file ownership: {e}")
            # Continue anyway if summaries table doesn't have the record yet (for backwards compatibility)

        # ===== CREDIT CHECK =====
        # Verify user has sufficient credits before starting generation
        user_profiles_table = dynamodb.Table(USER_PROFILES_TABLE)
        try:
            profile_response = user_profiles_table.get_item(Key={'userId': user_id})
            if 'Item' in profile_response:
                profile = profile_response['Item']
                subscription_tier = profile.get('subscriptionTier', 'free')

                # Unlimited tier always allowed
                if subscription_tier != 'unlimited':
                    credits_remaining = int(profile.get('creditsRemaining', 3))

                    if credits_remaining <= 0:
                        print(f"User {user_id} has no credits remaining (tier: {subscription_tier})")
                        return {
                            "statusCode": 403,
                            "headers": CORS_HEADERS,
                            "body": json.dumps({
                                "error": "Insufficient credits",
                                "message": "You have no credits remaining. Please upgrade to continue.",
                                "creditsRemaining": 0
                            })
                        }

                    print(f"User {user_id} has {credits_remaining} credits remaining")
                else:
                    print(f"User {user_id} has unlimited tier")
            else:
                # No profile found - default to free tier with 3 credits
                print(f"No profile found for user {user_id}, allowing generation with default credits")
        except Exception as e:
            print(f"Error checking credits: {e}")
            # Fail open for backwards compatibility - allow generation if credit check fails
            print("Allowing generation despite credit check error")

        # Generate unique job ID
        job_id = str(uuid.uuid4())

        # Calculate TTL (24 hours from now)
        ttl = int(time.time()) + 86400

        # Create job entry in DynamoDB
        table = dynamodb.Table(GENERATION_JOBS_TABLE)
        table.put_item(Item={
            'jobId': job_id,
            'userId': user_id,  # Store userId for data isolation
            'fileId': file_id,
            'jobDescription': job_description,
            'status': 'PROCESSING',
            'createdAt': int(time.time()),
            'ttl': ttl
        })

        print(f"Created generation job: {job_id} for user: {user_id}")

        # Invoke processing Lambda asynchronously
        lambda_client.invoke(
            FunctionName=PROCESS_GENERATION_FUNCTION_NAME,
            InvocationType='Event',  # Async invocation
            Payload=json.dumps({
                'jobId': job_id,
                'userId': user_id,  # Pass userId to processing Lambda
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
