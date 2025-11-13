import json
import os
import boto3
from datetime import datetime

# Initialize AWS clients
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['USER_PROFILES_TABLE'])

def lambda_handler(event, context):
    """
    Handle credit pack purchases via Dodo Payments webhook
    POST /user/subscription

    Expected webhook payload:
    {
        "userId": "user123",
        "productId": "prod_xxx",  # Dodo product ID
        "credits": 50,  # Number of credits purchased (20, 50, 150, or 500)
        "amount": 995,  # Amount paid in cents ($9.95)
        "paymentId": "pay_xxx",  # Dodo payment ID
        "dodoCustomerId": "cus_xxx"  # Dodo customer ID (optional)
    }
    """

    try:
        # Parse request body (simplified webhook handling following Dodo demo pattern)
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

        # Validate required fields for credit purchase
        if 'credits' not in body or 'productId' not in body:
            return {
                'statusCode': 400,
                'headers': {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'Content-Type',
                },
                'body': json.dumps({
                    'error': 'Missing required fields: credits and productId'
                })
            }

        credits_to_add = int(body['credits'])
        product_id = body['productId']
        amount = body.get('amount', 0)
        payment_id = body.get('paymentId', 'unknown')

        print(f"Processing credit pack purchase for user {user_id}: {credits_to_add} credits")

        # Get existing profile or create new one
        response = table.get_item(Key={'userId': user_id})

        if 'Item' not in response:
            # First-time purchaser - create new profile (this should rarely happen now)
            current_credits = 3  # Start with free credits
            total_purchased = 0
            purchase_history = []
            existing_item = {}
            print(f"Creating new profile for user {user_id}")
        else:
            # Existing user - preserve all existing fields
            existing_item = response['Item']
            current_credits = int(existing_item.get('creditsRemaining', 3))
            total_purchased = int(existing_item.get('totalCreditsPurchased', 0))
            purchase_history = existing_item.get('purchaseHistory', [])

        # Calculate new balances
        new_credits = current_credits + credits_to_add
        new_total_purchased = total_purchased + credits_to_add

        # Add purchase to history
        purchase_record = {
            'productId': product_id,
            'credits': credits_to_add,
            'amount': amount,
            'paymentId': payment_id,
            'purchaseDate': datetime.utcnow().isoformat()
        }
        purchase_history.append(purchase_record)

        # Start with existing profile data to preserve all fields
        profile_item = dict(existing_item) if existing_item else {}

        # Update/add credit-related fields
        profile_item['userId'] = user_id
        profile_item['creditsRemaining'] = new_credits
        profile_item['totalCreditsPurchased'] = new_total_purchased
        profile_item['lastPurchaseProductId'] = product_id
        profile_item['lastPurchaseCredits'] = credits_to_add
        profile_item['lastPurchaseAmount'] = amount
        profile_item['lastPurchaseDate'] = datetime.utcnow().isoformat()
        profile_item['lastPaymentId'] = payment_id
        profile_item['purchaseHistory'] = purchase_history
        profile_item['updatedAt'] = datetime.utcnow().isoformat()

        # Keep createdAt if it exists
        if 'createdAt' not in profile_item:
            profile_item['createdAt'] = datetime.utcnow().isoformat()

        # Store customer ID if provided
        if 'dodoCustomerId' in body:
            profile_item['dodoCustomerId'] = body['dodoCustomerId']

        # Save to DynamoDB
        table.put_item(Item=profile_item)

        print(f"✓ Credits updated: {current_credits} → {new_credits} (+{credits_to_add})")
        print(f"✓ Total lifetime purchases: {new_total_purchased} credits")

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
