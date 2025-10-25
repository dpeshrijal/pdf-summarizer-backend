import json
import os
import boto3
import google.generativeai as genai
from pinecone import Pinecone
from fpdf import FPDF # <-- NEW IMPORT
import base64 # <-- NEW IMPORT

# =================================================================
# Initialize Clients
# =================================================================
ssm = boto3.client('ssm')

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "OPTIONS,POST,GET"
}

def get_ssm_parameter(parameter_name):
    response = ssm.get_parameter(Name=parameter_name, WithDecryption=True)
    return response['Parameter']['Value']

try:
    gemini_api_key = get_ssm_parameter("/pdf-summarizer/gemini-api-key")
    genai.configure(api_key=gemini_api_key)
    
    pinecone_api_key = get_ssm_parameter("/pdf-summarizer/pinecone-api-key")
    pc = Pinecone(api_key=pinecone_api_key)
    
    PINECONE_INDEX_NAME = "resume-embeddings" 
    index = pc.Index(PINECONE_INDEX_NAME)

    generative_model = genai.GenerativeModel('gemini-2.5-pro')

except Exception as e:
    print(f"FATAL: Could not initialize services. Error: {e}")
    raise e

# =================================================================
# PDF Generation Helper
# =================================================================
class PDF(FPDF):
    def header(self):
        self.set_font('Helvetica', 'B', 12)
        self.cell(0, 10, 'Your Tailored Document', 0, 1, 'C')

    def chapter_title(self, title):
        self.set_font('Helvetica', 'B', 12)
        self.cell(0, 10, title, 0, 1, 'L')
        self.ln(5)

    def chapter_body(self, body):
        self.set_font('Helvetica', '', 10)
        # Using multi_cell to handle line breaks and long text
        self.multi_cell(0, 5, body)
        self.ln()

# =================================================================
# Main Lambda Handler
# =================================================================
def lambda_handler(event, context):
    try:
        body = json.loads(event.get('body', '{}'))
        job_description = body.get('jobDescription')
        file_id = body.get('fileId')

        if not job_description or not file_id:
            # Return error as JSON
            return {
                "statusCode": 400, "headers": CORS_HEADERS,
                "body": json.dumps({"error": "jobDescription and fileId are required"})
            }

        # --- (Steps 1-3: Embedding, Querying Pinecone, Prompting Gemini) ---
        # This part remains the same as before...
        query_embedding = genai.embed_content(model="models/text-embedding-004", content=job_description, task_type="RETRIEVAL_QUERY")['embedding']
        query_response = index.query(vector=query_embedding, top_k=5, include_metadata=True, filter={"original_file_id": {"$eq": file_id}})
        if not query_response['matches']:
             raise ValueError("Could not find any relevant sections in the master resume.")
        context_chunks = [match['metadata']['text'] for match in query_response['matches']]
        resume_context = "\n---\n".join(context_chunks)
        
        prompt = f"""
        You are a professional resume and cover letter writing assistant... (and so on, same prompt as before)
        ...Provide the output in a single, valid JSON object with two keys: "tailoredResume" and "coverLetter".
        """
        
        print("Generating document text with Gemini...")
        response = generative_model.generate_content(prompt)
        cleaned_response_text = response.text.strip().replace('```json', '').replace('```', '')
        generated_docs = json.loads(cleaned_response_text)

        # 4. --- NEW LOGIC: Generate the PDF in memory ---
        print("Generating PDF document...")
        pdf = PDF()
        pdf.add_page()
        
        # Add Cover Letter
        pdf.chapter_title('Cover Letter')
        # We must encode the text properly to handle special characters
        pdf.chapter_body(generated_docs['coverLetter'].encode('latin-1', 'replace').decode('latin-1'))
        
        pdf.add_page()
        
        # Add Tailored Resume
        pdf.chapter_title('Tailored Resume')
        pdf.chapter_body(generated_docs['tailoredResume'].encode('latin-1', 'replace').decode('latin-1'))
        
        # Get the PDF content as a byte string
        pdf_output_bytes = pdf.output(dest='S').encode('latin-1')
        
        # 5. --- NEW LOGIC: Encode the PDF for API Gateway ---
        # We encode the binary PDF data into a Base64 string to safely send it in a JSON response
        pdf_base64 = base64.b64encode(pdf_output_bytes).decode('utf-8')
        
        print("PDF generated and encoded. Returning to client.")
        
        return {
            "statusCode": 200,
            "headers": {
                **CORS_HEADERS,
                # Tell the browser that the response is a PDF file
                "Content-Type": "application/pdf",
                "Content-Disposition": "attachment; filename=\"tailored_documents.pdf\""
            },
            # Return the base64-encoded PDF directly in the body
            "body": pdf_base64,
            "isBase64Encoded": True # This flag is crucial for API Gateway
        }

    except Exception as e:
        print(f"Error generating documents: {e}")
        # Return error as JSON, even on the PDF endpoint
        return {
            "statusCode": 500, "headers": CORS_HEADERS,
            "body": json.dumps({"error": f"Failed to generate documents: {str(e)}"})
        }