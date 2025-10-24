import boto3
import fitz  # PyMuPDF
import json
import os
import google.generativeai as genai
import urllib.parse
import uuid

s3 = boto3.client('s3')
ssm = boto3.client('ssm')
dynamodb = boto3.resource('dynamodb')

TABLE_NAME = os.environ.get('TABLE_NAME')
table = dynamodb.Table(TABLE_NAME)

PARAMETER_NAME = "/pdf-summarizer/gemini-api-key"
try:
    api_key_param = ssm.get_parameter(Name=PARAMETER_NAME, WithDecryption=True)
    GEMINI_API_KEY = api_key_param['Parameter']['Value']
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-2.5-flash-lite')
except Exception as e:
    print(f"FATAL: Could not initialize Gemini client. Error: {e}")
    raise e

def lambda_handler(event, context):
    try:
        s3_event = event['Records'][0]['s3']
        bucket_name = s3_event['bucket']['name']
        file_key = urllib.parse.unquote_plus(s3_event['object']['key'], encoding='utf-8')
        
        print(f"Processing file: {file_key} from bucket: {bucket_name}")

        head_response = s3.head_object(Bucket=bucket_name, Key=file_key)
        metadata = head_response.get('Metadata', {})
        job_id = metadata.get('fileid')

        if not job_id:
            print(f"FATAL ERROR: 'fileid' not found in S3 metadata for object: {file_key}")
            return {'statusCode': 400, 'body': json.dumps('Missing fileid in metadata')}
        
        print(f"Retrieved fileId from metadata: {job_id}")
        
        original_filename = os.path.basename(file_key)

        download_path = f'/tmp/{job_id}-{original_filename}'
        s3.download_file(bucket_name, file_key, download_path)
        print(f"Successfully downloaded file to: {download_path}")

        doc = fitz.open(download_path)
        full_text = "".join(page.get_text() for page in doc)
        doc.close()
        
        if not full_text.strip():
            print(f"No text found in PDF: {file_key}. Updating status to FAILED.")
            table.update_item(
                Key={'fileId': job_id},
                UpdateExpression="set processingStatus = :p, summary = :s",
                ExpressionAttributeValues={':p': 'FAILED', ':s': 'No text could be extracted from the PDF.'}
            )
            return {'statusCode': 400, 'body': json.dumps('No text found in PDF.')}

        prompt = f"Please provide a concise, professional summary of the following document:\n\n{full_text}"
        print("Sending text to Gemini for summarization...")
        response = model.generate_content(prompt)
        summary = response.text
        
        print(f"Updating DynamoDB for fileId: {job_id}")
        table.update_item(
            Key={'fileId': job_id},
            UpdateExpression="set summary = :s, processingStatus = :p",
            ExpressionAttributeValues={
                ':s': summary,
                ':p': 'COMPLETED'
            }
        )
        print("Successfully updated DynamoDB.")

        return {
            'statusCode': 200,
            'body': json.dumps(f'Summary for {original_filename} saved successfully!')
        }
    except Exception as e:
        print(f"Error processing file: {e}")
        job_id_on_error = locals().get('job_id')
        if job_id_on_error:
            try:
                table.update_item(
                    Key={'fileId': job_id_on_error},
                    UpdateExpression="set processingStatus = :p, summary = :s",
                    ExpressionAttributeValues={':p': 'FAILED', ':s': str(e)}
                )
            except Exception as db_error:
                print(f"Could not update DynamoDB with error status: {db_error}")
        return {'statusCode': 500, 'body': json.dumps(f'Error processing file: {str(e)}')}