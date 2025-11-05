import json
import os
import boto3

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['USER_PROFILES_TABLE'])

def lambda_handler(event, context):
    """
    Get user profile data
    GET /user/profile?userId=user123

    Returns:
    {
        "hasProfile": true,
        "profile": { ... }
    }
    """

    try:
        # Get userId from query parameters
        query_params = event.get('queryStringParameters') or {}
        user_id = query_params.get('userId')

        if not user_id:
            return {
                'statusCode': 400,
                'headers': {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'Content-Type',
                },
                'body': json.dumps({
                    'error': 'Missing userId parameter'
                })
            }

        # Get profile from DynamoDB
        response = table.get_item(Key={'userId': user_id})

        if 'Item' in response:
            return {
                'statusCode': 200,
                'headers': {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'Content-Type',
                },
                'body': json.dumps({
                    'hasProfile': True,
                    'profile': response['Item']
                })
            }
        else:
            return {
                'statusCode': 200,
                'headers': {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'Content-Type',
                },
                'body': json.dumps({
                    'hasProfile': False,
                    'profile': None
                })
            }

    except Exception as e:
        print(f"Error getting profile: {str(e)}")
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
