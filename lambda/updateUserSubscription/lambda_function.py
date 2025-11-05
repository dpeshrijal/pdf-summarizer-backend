import json
import os
import boto3
from datetime import datetime

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['USER_PROFILES_TABLE'])

def lambda_handler(event, context):
    """
    Update user subscription data
    POST /user/subscription

    Expected body:
    {
        "userId": "user123",
        "subscriptionTier": "pro",  # free, pro, unlimited
        "subscriptionStatus": "active",  # active, cancelled, paused, past_due
        "subscriptionId": "sub_xxx",  # Dodo subscription ID
        "dodoCustomerId": "cus_xxx",  # Dodo customer ID
        "creditsRemaining": 200,
        "creditsLimit": 200,
        "billingCycleStart": "2025-01-01T00:00:00Z",
        "billingCycleEnd": "2025-02-01T00:00:00Z",
        "lastPaymentId": "pay_xxx",
        "lastPaymentDate": "2025-01-01T00:00:00Z",
        "cancelledAt": "2025-01-15T00:00:00Z",  # optional
        "refundedAt": "2025-01-15T00:00:00Z"  # optional
    }
    """

    try:
        # Parse request body
        body = json.loads(event.get('body', '{}'))

        # Extract userId (required)
        user_id = body.get('userId')

        if not user_id:
            return {
                'statusCode': 400,
                'headers': {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'Content-Type',
                },
                'body': json.dumps({
                    'error': 'Missing required field: userId'
                })
            }

        # Get existing profile
        response = table.get_item(Key={'userId': user_id})

        if 'Item' not in response:
            # Create new profile if it doesn't exist
            profile_item = {
                'userId': user_id,
                'createdAt': datetime.utcnow().isoformat(),
                'updatedAt': datetime.utcnow().isoformat(),
            }
        else:
            profile_item = response['Item']
            profile_item['updatedAt'] = datetime.utcnow().isoformat()

        # Update subscription fields
        subscription_fields = [
            'subscriptionTier',
            'subscriptionStatus',
            'subscriptionId',
            'dodoCustomerId',
            'creditsRemaining',
            'creditsLimit',
            'billingCycleStart',
            'billingCycleEnd',
            'lastPaymentId',
            'lastPaymentDate',
            'cancelledAt',
            'refundedAt',
        ]

        for field in subscription_fields:
            if field in body:
                profile_item[field] = body[field]

        # Save to DynamoDB
        table.put_item(Item=profile_item)

        return {
            'statusCode': 200,
            'headers': {
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Headers': 'Content-Type',
            },
            'body': json.dumps({
                'success': True,
                'profile': profile_item
            })
        }

    except json.JSONDecodeError:
        return {
            'statusCode': 400,
            'headers': {
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Headers': 'Content-Type',
            },
            'body': json.dumps({
                'error': 'Invalid JSON in request body'
            })
        }
    except Exception as e:
        print(f"Error updating subscription: {str(e)}")
        return {
            'statusCode': 500,
            'headers': {
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Headers': 'Content-Type',
            },
            'body': json.dumps({
                'error': 'Internal server error',
                'details': str(e)
            })
        }
