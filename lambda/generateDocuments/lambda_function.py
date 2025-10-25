import json
import os
import boto3
import google.generativeai as genai
from pinecone import Pinecone
from fpdf import FPDF
import base64

# Initialization
s3 = boto3.client('s3')
ssm = boto3.client('ssm')
dynamodb = boto3.resource('dynamodb')

CORS_HEADERS = { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "OPTIONS,POST,GET" }
TABLE_NAME = os.environ.get('TABLE_NAME')
BUCKET_NAME = os.environ.get('BUCKET_NAME')
table = dynamodb.Table(TABLE_NAME)

def get_ssm_parameter(parameter_name):
    response = ssm.get_parameter(Name=parameter_name, WithDecryption=True)
    return response['Parameter']['Value']

try:
    gemini_api_key = get_ssm_parameter("/pdf-summarizer/gemini-api-key")
    genai.configure(api_key=gemini_api_key)
    
    pinecone_api_key = get_ssm_parameter("/pdf-summarizer/pinecone-api-key")
    pinecone_env = get_ssm_parameter("/pdf-summarizer/pinecone-environment")
    pc = Pinecone(api_key=pinecone_api_key, environment=pinecone_env)
    
    PINECONE_INDEX_NAME = "resume-embeddings" 
    index = pc.Index(PINECONE_INDEX_NAME)

    generative_model = genai.GenerativeModel('gemini-2.5-flash')

except Exception as e:
    print(f"FATAL: Could not initialize services. Error: {e}")
    raise e

# PDF Helper Class
class PDF(FPDF):
    def header(self): self.set_font('Helvetica', 'B', 12); self.cell(0, 10, 'Your Tailored Documents', 0, 1, 'C')
    def chapter_title(self, title): self.set_font('Helvetica', 'B', 12); self.cell(0, 10, title, 0, 1, 'L'); self.ln(5)
    def chapter_body(self, body): self.set_font('Helvetica', '', 11); self.multi_cell(0, 5, body.encode('latin-1', 'replace').decode('latin-1')); self.ln()

# Main Lambda Handler
def lambda_handler(event, context):
    try:
        body = json.loads(event.get('body', '{}'))
        job_description = body.get('jobDescription')
        file_id = body.get('fileId')

        if not job_description or not file_id:
            return { "statusCode": 400, "headers": CORS_HEADERS, "body": json.dumps({"error": "jobDescription and fileId are required"}) }

        # RAG Pipeline (remains the same)
        query_embedding = genai.embed_content(model="models/text-embedding-004", content=job_description, task_type="RETRIEVAL_QUERY")['embedding']
        query_response = index.query(vector=query_embedding, top_k=5, include_metadata=True, filter={"original_file_id": {"$eq": file_id}})
        if not query_response['matches']: raise ValueError("Could not find any relevant sections in the master resume.")
        
        context_chunks = [match['metadata']['text'] for match in query_response['matches']]
        resume_context = "\n---\n".join(context_chunks)
        
        prompt = f"""... (same prompt as before) ..."""
        
        response = generative_model.generate_content(prompt)
        if not response.text: raise ValueError("The response from the AI was blocked or empty.")

        cleaned_response_text = response.text.strip().replace('```json', '').replace('```', '')
        generated_docs = json.loads(cleaned_response_text)

        # PDF Generation
        pdf = PDF()
        pdf.add_page(); pdf.chapter_title('Cover Letter'); pdf.chapter_body(generated_docs['coverLetter'])
        pdf.add_page(); pdf.chapter_title('Tailored Resume'); pdf.chapter_body(generated_docs['tailoredResume'])
        
        pdf_output_bytes = pdf.output()

        # --- NEW LOGIC: SAVE PDF TO S3 ---
        s3_key_for_generated_pdf = f"generated/{file_id}-tailored-documents.pdf"
        print(f"Uploading generated PDF to s3://{BUCKET_NAME}/{s3_key_for_generated_pdf}")
        s3.put_object(
            Bucket=BUCKET_NAME,
            Key=s3_key_for_generated_pdf,
            Body=pdf_output_bytes,
            ContentType='application/pdf'
        )
        print("Successfully uploaded PDF to S3.")
        
        # --- NEW LOGIC: UPDATE DYNAMODB WITH S3 KEY ---
        print("Updating DynamoDB with generated PDF location...")
        table.update_item(
            Key={'fileId': file_id},
            UpdateExpression="set processingStatus = :p, generatedPdfKey = :k",
            ExpressionAttributeValues={
                ':p': 'COMPLETED',
                ':k': s3_key_for_generated_pdf
            }
        )

        # Immediately return a success message. Do not send the file.
        return { "statusCode": 200, "headers": CORS_HEADERS, "body": json.dumps({"message": "Successfully started generation."}) }

    except Exception as e:
        # Update DynamoDB with failure status
        # (You would add logic here to update the DB item to 'FAILED')
        print(f"Error generating documents: {e}")
        return { "statusCode": 500, "headers": CORS_HEADERS, "body": json.dumps({"error": f"Failed to generate documents: {str(e)}"}) }