import boto3
import fitz  # PyMuPDF
import json
import os
import google.generativeai as genai
import urllib.parse
import uuid
from pinecone import Pinecone

# =================================================================
# Initialize Clients (done once per cold start)
# =================================================================
s3 = boto3.client('s3')
ssm = boto3.client('ssm')
dynamodb = boto3.resource('dynamodb')

TABLE_NAME = os.environ.get('TABLE_NAME')
table = dynamodb.Table(TABLE_NAME)

def get_ssm_parameter(parameter_name):
    """Helper function to get a SecureString parameter from SSM."""
    response = ssm.get_parameter(Name=parameter_name, WithDecryption=True)
    return response['Parameter']['Value']

try:
    # Gemini API Configuration
    gemini_api_key = get_ssm_parameter("/pdf-summarizer/gemini-api-key")
    genai.configure(api_key=gemini_api_key)
    
    # Pinecone API Configuration
    pinecone_api_key = get_ssm_parameter("/pdf-summarizer/pinecone-api-key")
    pinecone_env = get_ssm_parameter("/pdf-summarizer/pinecone-environment")
    pc = Pinecone(api_key=pinecone_api_key, environment=pinecone_env)
    
    PINECONE_INDEX_NAME = "resume-embeddings" 
    index = pc.Index(PINECONE_INDEX_NAME)

except Exception as e:
    print(f"FATAL: Could not initialize one or more services. Error: {e}")
    raise e

# =================================================================
# Helper Functions
# =================================================================
def chunk_text(text, chunk_size=1000, chunk_overlap=100):
    """Splits text into overlapping chunks."""
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += chunk_size - chunk_overlap
    return chunks

def get_embedding(text_chunk):
    """Generates an embedding for a text chunk using Google's model."""
    try:
        # Note: The model name for embeddings is different from the generative model
        result = genai.embed_content(
            model="models/text-embedding-004",
            content=text_chunk,
            task_type="RETRIEVAL_DOCUMENT" 
        )
        return result['embedding']
    except Exception as e:
        print(f"Error generating embedding for chunk: {e}")
        return None

# =================================================================
# Main Lambda Handler
# =================================================================
def lambda_handler(event, context):
    try:
        s3_event = event['Records'][0]['s3']
        bucket_name = s3_event['bucket']['name']
        file_key = urllib.parse.unquote_plus(s3_event['object']['key'], encoding='utf-8')
        
        print(f"Processing master resume: {file_key}")

        head_response = s3.head_object(Bucket=bucket_name, Key=file_key)
        metadata = head_response.get('Metadata', {})
        job_id = metadata.get('fileid')

        if not job_id:
            raise ValueError("'fileid' not found in S3 metadata.")
        
        print(f"Retrieved fileId from metadata: {job_id}")

        download_path = f'/tmp/{job_id}.pdf'
        s3.download_file(bucket_name, file_key, download_path)
        
        doc = fitz.open(download_path)
        full_text = "".join(page.get_text() for page in doc).strip()
        doc.close()

        if not full_text:
            raise ValueError("No text could be extracted from the PDF.")

        text_chunks = chunk_text(full_text)
        print(f"Split text into {len(text_chunks)} chunks.")

        vectors_to_upsert = []
        for i, chunk in enumerate(text_chunks):
            embedding = get_embedding(chunk)
            if embedding:
                vector_id = f"{job_id}-{i}"
                vectors_to_upsert.append({
                    "id": vector_id,
                    "values": embedding,
                    "metadata": {"text": chunk, "original_file_id": job_id}
                })

        if vectors_to_upsert:
            print(f"Upserting {len(vectors_to_upsert)} vectors to Pinecone index '{PINECONE_INDEX_NAME}'...")
            for i in range(0, len(vectors_to_upsert), 100):
                batch = vectors_to_upsert[i:i+100]
                index.upsert(vectors=batch)
            print("Successfully upserted vectors to Pinecone.")

        table.update_item(
            Key={'fileId': job_id},
            UpdateExpression="set processingStatus = :p",
            ExpressionAttributeValues={':p': 'READY_FOR_QUERY'}
        )
        print("Successfully updated DynamoDB status to READY_FOR_QUERY.")

        return {'statusCode': 200, 'body': json.dumps('Resume processed and indexed successfully!')}

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