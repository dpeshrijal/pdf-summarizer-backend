import json
import os
import boto3
from datetime import datetime
from svix.webhooks import Webhook, WebhookVerificationError

# Initialize AWS clients
ssm = boto3.client('ssm')
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['USER_PROFILES_TABLE'])

def get_ssm_parameter(parameter_name):
    """Helper function to get a SecureString parameter from SSM."""
    response = ssm.get_parameter(Name=parameter_name, WithDecryption=True)
    return response['Parameter']['Value']

# Get webhook secret at cold start
try:
    WEBHOOK_SECRET = get_ssm_parameter("/pdf-summarizer/dodo-webhook-secret")
except Exception as e:
    print(f"WARNING: Could not load webhook secret from SSM: {e}")
    WEBHOOK_SECRET = None

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
        # ===== WEBHOOK SIGNATURE VERIFICATION =====
        # Verify the webhook signature to ensure it came from Dodo Payments
        if WEBHOOK_SECRET:
            headers = event.get('headers', {})

            # Extract webhook headers (case-insensitive)
            webhook_id = headers.get('webhook-id') or headers.get('Webhook-Id')
            webhook_signature = headers.get('webhook-signature') or headers.get('Webhook-Signature')
            webhook_timestamp = headers.get('webhook-timestamp') or headers.get('Webhook-Timestamp')

            if not all([webhook_id, webhook_signature, webhook_timestamp]):
                print("Missing webhook headers - rejecting request")
                return {
                    'statusCode': 401,
                    'headers': {
                        'Access-Control-Allow-Origin': '*',
                        'Access-Control-Allow-Headers': 'Content-Type',
                    },
                    'body': json.dumps({
                        'error': 'Missing webhook verification headers'
                    })
                }

            # Verify webhook signature using svix library (same as standardwebhooks)
            wh = Webhook(WEBHOOK_SECRET)
            payload = event.get('body', '')

            try:
                wh.verify(payload, {
                    "webhook-id": webhook_id,
                    "webhook-signature": webhook_signature,
                    "webhook-timestamp": webhook_timestamp,
                })
                print("Webhook signature verified successfully")
            except WebhookVerificationError as e:
                print(f"Webhook verification failed: {e}")
                return {
                    'statusCode': 401,
                    'headers': {
                        'Access-Control-Allow-Origin': '*',
                        'Access-Control-Allow-Headers': 'Content-Type',
                    },
                    'body': json.dumps({
                        'error': 'Webhook signature verification failed'
                    })
                }
        else:
            print("WARNING: Webhook secret not configured - skipping verification")

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
