import json
import os
import boto3
from datetime import datetime
from decimal import Decimal
import re

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['USER_PROFILES_TABLE'])

def decimal_to_number(obj):
    """Convert DynamoDB Decimal types to int or float for JSON serialization"""
    if isinstance(obj, list):
        return [decimal_to_number(i) for i in obj]
    elif isinstance(obj, dict):
        return {k: decimal_to_number(v) for k, v in obj.items()}
    elif isinstance(obj, Decimal):
        # Convert to int if no decimal places, otherwise float
        if obj % 1 == 0:
            return int(obj)
        else:
            return float(obj)
    else:
        return obj

def lambda_handler(event, context):
    """
    Save or update user profile data
    POST /user/profile

    Expected body:
    {
        "userId": "user123",
        "name": "John Doe",
        "email": "john@example.com",
        "phone": "+1234567890",  # optional
        "location": "San Francisco, CA",  # optional
        "linkedinUrl": "https://linkedin.com/in/johndoe",  # optional
        "githubUrl": "https://github.com/johndoe",  # optional
        "portfolioUrl": "https://johndoe.com",  # optional
        "customUrl": "https://medium.com/@johndoe",  # optional
        "customUrlLabel": "Medium"  # optional
    }
    """

    try:
        # Parse request body
        body = json.loads(event.get('body', '{}'))

        # Extract and validate required fields
        user_id = body.get('userId')
        name = body.get('name')
        email = body.get('email')

        if not user_id or not name or not email:
            return {
                'statusCode': 400,
                'headers': {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'Content-Type',
                },
                'body': json.dumps({
                    'error': 'Missing required fields: userId, name, email'
                })
            }

        # Validate email format
        email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if not re.match(email_pattern, email):
            return {
                'statusCode': 400,
                'headers': {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'Content-Type',
                },
                'body': json.dumps({
                    'error': 'Invalid email format'
                })
            }

        # Extract optional fields
        phone = body.get('phone')
        location = body.get('location')
        linkedin_url = body.get('linkedinUrl')
        github_url = body.get('githubUrl')
        portfolio_url = body.get('portfolioUrl')
        custom_url = body.get('customUrl')
        custom_url_label = body.get('customUrlLabel')
        onboarding_complete = body.get('onboardingComplete')

        # Validate URLs if provided
        url_pattern = r'^https?://.+'
        urls_to_validate = {
            'linkedinUrl': linkedin_url,
            'githubUrl': github_url,
            'portfolioUrl': portfolio_url,
            'customUrl': custom_url,
        }

        for field_name, url in urls_to_validate.items():
            if url and not re.match(url_pattern, url):
                return {
                    'statusCode': 400,
                    'headers': {
                        'Access-Control-Allow-Origin': '*',
                        'Access-Control-Allow-Headers': 'Content-Type',
                    },
                    'body': json.dumps({
                        'error': f'Invalid URL format for {field_name}'
                    })
                }

        # Build the profile item
        timestamp = datetime.utcnow().isoformat()

        profile_item = {
            'userId': user_id,
            'name': name,
            'email': email,
            'updatedAt': timestamp,
        }

        # Check if this is a new profile (for createdAt and preserving existing fields)
        try:
            existing = table.get_item(Key={'userId': user_id})
            if 'Item' in existing:
                existing_item = existing['Item']
                profile_item['createdAt'] = existing_item.get('createdAt', timestamp)

                # Preserve credit-related fields if they exist
                if 'creditsRemaining' in existing_item:
                    profile_item['creditsRemaining'] = existing_item['creditsRemaining']
                if 'totalCreditsPurchased' in existing_item:
                    profile_item['totalCreditsPurchased'] = existing_item['totalCreditsPurchased']
                if 'lastPurchaseProductId' in existing_item:
                    profile_item['lastPurchaseProductId'] = existing_item['lastPurchaseProductId']
                if 'lastPurchaseCredits' in existing_item:
                    profile_item['lastPurchaseCredits'] = existing_item['lastPurchaseCredits']
                if 'lastPurchaseAmount' in existing_item:
                    profile_item['lastPurchaseAmount'] = existing_item['lastPurchaseAmount']
                if 'lastPurchaseDate' in existing_item:
                    profile_item['lastPurchaseDate'] = existing_item['lastPurchaseDate']
                if 'lastPaymentId' in existing_item:
                    profile_item['lastPaymentId'] = existing_item['lastPaymentId']
                if 'dodoCustomerId' in existing_item:
                    profile_item['dodoCustomerId'] = existing_item['dodoCustomerId']

                # Preserve onboardingComplete if not explicitly provided in request
                if onboarding_complete is None and 'onboardingComplete' in existing_item:
                    profile_item['onboardingComplete'] = existing_item['onboardingComplete']
            else:
                profile_item['createdAt'] = timestamp
        except Exception:
            profile_item['createdAt'] = timestamp

        # Add optional fields if provided
        if phone:
            profile_item['phone'] = phone
        if location:
            profile_item['location'] = location
        if linkedin_url:
            profile_item['linkedinUrl'] = linkedin_url
        if github_url:
            profile_item['githubUrl'] = github_url
        if portfolio_url:
            profile_item['portfolioUrl'] = portfolio_url
        if custom_url:
            profile_item['customUrl'] = custom_url
        if custom_url_label:
            profile_item['customUrlLabel'] = custom_url_label
        if onboarding_complete is not None:
            profile_item['onboardingComplete'] = onboarding_complete

        # Save to DynamoDB
        table.put_item(Item=profile_item)

        # Convert Decimal types before returning
        profile_response = decimal_to_number(profile_item)

        return {
            'statusCode': 200,
            'headers': {
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Headers': 'Content-Type',
            },
            'body': json.dumps({
                'success': True,
                'profile': profile_response
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
        print(f"Error saving profile: {str(e)}")
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
