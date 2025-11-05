import json
import os
import boto3
from datetime import datetime
import re

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['USER_PROFILES_TABLE'])

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

        # Check if this is a new profile (for createdAt)
        try:
            existing = table.get_item(Key={'userId': user_id})
            if 'Item' in existing:
                profile_item['createdAt'] = existing['Item'].get('createdAt', timestamp)
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
