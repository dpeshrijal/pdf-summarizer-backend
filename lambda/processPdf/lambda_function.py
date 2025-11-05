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
    
    # This is the name you gave your index in the Pinecone console
    PINECONE_INDEX_NAME = "resume-embeddings" 
    index = pc.Index(PINECONE_INDEX_NAME)

except Exception as e:
    print(f"FATAL: Could not initialize one or more services. Error: {e}")
    raise e

# =================================================================
# Helper Functions
# =================================================================
def validate_resume_content(text):
    """
    Validates if the extracted text appears to be a resume/CV.
    Uses heuristics to check for resume-like content.
    """
    text_lower = text.lower()
    word_count = len(text.split())

    # Common resume keywords and sections
    resume_keywords = [
        'experience', 'education', 'skills', 'work', 'employment',
        'university', 'degree', 'bachelor', 'master', 'phd',
        'project', 'responsibilities', 'achievements', 'accomplishments',
        'certification', 'training', 'qualification', 'professional',
        'career', 'resume', 'curriculum vitae', 'cv', 'objective'
    ]

    # Contact information patterns (common in resumes)
    contact_indicators = [
        'email', 'phone', 'linkedin', 'github', 'portfolio',
        '@', 'tel:', 'mobile', 'address'
    ]

    # Technical/professional terms (common in resumes)
    professional_terms = [
        'developed', 'managed', 'led', 'implemented', 'designed',
        'created', 'built', 'analyzed', 'coordinated', 'collaborated',
        'programming', 'software', 'engineer', 'developer', 'analyst',
        'manager', 'specialist', 'consultant', 'director'
    ]

    # Count matches
    keyword_matches = sum(1 for keyword in resume_keywords if keyword in text_lower)
    contact_matches = sum(1 for indicator in contact_indicators if indicator in text_lower)
    professional_matches = sum(1 for term in professional_terms if term in text_lower)

    # Validation logic
    if word_count < 50:
        return {
            "is_valid": False,
            "reason": "The document is too short to be a resume. Please upload a complete resume with your work experience, education, and skills."
        }

    if keyword_matches < 3 and contact_matches < 2 and professional_matches < 3:
        return {
            "is_valid": False,
            "reason": "This document doesn't appear to contain resume information. Please upload a PDF with your professional experience, skills, and education."
        }

    # Additional check: Does it look like a book, article, or other non-resume content?
    # Books/articles typically have chapters, abstracts, references
    non_resume_indicators = ['chapter', 'abstract', 'references', 'bibliography', 'introduction', 'conclusion', 'figure', 'table of contents']
    non_resume_matches = sum(1 for indicator in non_resume_indicators if indicator in text_lower)

    if non_resume_matches >= 4 and keyword_matches < 5:
        return {
            "is_valid": False,
            "reason": "This appears to be an academic paper, book, or article rather than a resume. Please upload your professional resume or CV."
        }

    return {"is_valid": True, "reason": ""}

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
            task_type="RETRIEVAL_DOCUMENT" # Important for storing documents
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

        # Extract userId from S3 key path: user-{userId}/{fileId}-{filename}
        # This ensures proper data isolation in Pinecone
        user_id = None
        if file_key.startswith('user-'):
            user_id = file_key.split('/')[0].replace('user-', '')

        if not user_id:
            raise ValueError(f"Could not extract userId from S3 key: {file_key}")

        print(f"Extracted userId: {user_id}")

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

        # Validate if this is actually a resume
        validation_result = validate_resume_content(full_text)
        if not validation_result["is_valid"]:
            raise ValueError(f"NOT_A_RESUME: {validation_result['reason']}")

        # 1. Chunk the extracted text
        text_chunks = chunk_text(full_text)
        print(f"Split text into {len(text_chunks)} chunks.")

        vectors_to_upsert = []
        for i, chunk in enumerate(text_chunks):
            # 2. Create an embedding for each chunk
            embedding = get_embedding(chunk)
            if embedding:
                # 3. Prepare the vector for Pinecone
                vector_id = f"{job_id}-{i}"
                vectors_to_upsert.append({
                    "id": vector_id,
                    "values": embedding,
                    "metadata": {
                        "text": chunk,
                        "original_file_id": job_id,
                        "user_id": user_id  # Critical: Store userId for data isolation
                    }
                })

        if vectors_to_upsert:
            # 4. Upsert the vectors to Pinecone in batches
            print(f"Upserting {len(vectors_to_upsert)} vectors to Pinecone index '{PINECONE_INDEX_NAME}'...")
            # Pinecone recommends upserting in batches for larger documents
            for i in range(0, len(vectors_to_upsert), 100): # Upsert in batches of 100
                batch = vectors_to_upsert[i:i+100]
                index.upsert(vectors=batch)
            print("Successfully upserted vectors to Pinecone.")

        # 5. Update DynamoDB to show the resume is ready for querying
        table.update_item(
            Key={'fileId': job_id},
            UpdateExpression="set processingStatus = :p",
            ExpressionAttributeValues={':p': 'READY_FOR_QUERY'}
        )
        print("Successfully updated DynamoDB status to READY_FOR_QUERY.")

        return {'statusCode': 200, 'body': json.dumps('Resume processed and indexed successfully!')}

    except Exception as e:
        error_message = str(e)
        print(f"Error processing file: {error_message}")

        # Check if this is a validation error
        is_validation_error = error_message.startswith("NOT_A_RESUME:")
        if is_validation_error:
            # Extract the user-friendly message
            user_message = error_message.replace("NOT_A_RESUME: ", "")
            print(f"Validation failed: {user_message}")
        else:
            user_message = error_message

        job_id_on_error = locals().get('job_id')
        if job_id_on_error:
            try:
                table.update_item(
                    Key={'fileId': job_id_on_error},
                    UpdateExpression="set processingStatus = :p, summary = :s, errorType = :t",
                    ExpressionAttributeValues={
                        ':p': 'FAILED',
                        ':s': user_message,
                        ':t': 'VALIDATION_ERROR' if is_validation_error else 'PROCESSING_ERROR'
                    }
                )
            except Exception as db_error:
                print(f"Could not update DynamoDB with error status: {db_error}")
        return {'statusCode': 500, 'body': json.dumps(f'Error processing file: {error_message}')}