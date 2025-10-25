import json
import os
import boto3
import google.generativeai as genai
from pinecone import Pinecone
from fpdf import FPDF
import base64

# =================================================================
# Initialize Clients & CORS Headers
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
    pinecone_env = get_ssm_parameter("/pdf-summarizer/pinecone-environment")
    pc = Pinecone(api_key=pinecone_api_key, environment=pinecone_env)
    
    PINECONE_INDEX_NAME = "resume-embeddings" 
    index = pc.Index(PINECONE_INDEX_NAME)

    generative_model = genai.GenerativeModel('gemini-2.5-flash')

except Exception as e:
    print(f"FATAL: Could not initialize services. Error: {e}")
    raise e

# =================================================================
# PDF Generation Helper
# =================================================================
class PDF(FPDF):
    def header(self):
        self.set_font('Helvetica', 'B', 12)
        self.cell(0, 10, 'Your Tailored Documents', 0, 1, 'C')

    def chapter_title(self, title):
        self.set_font('Helvetica', 'B', 12)
        self.cell(0, 10, title, 0, 1, 'L')
        self.ln(5)

    def chapter_body(self, body):
        self.set_font('Helvetica', '', 11)
        # Encode text to 'latin-1' and replace unsupported characters
        # This is a safe way to handle a wide range of text with standard PDF fonts.
        safe_body = body.encode('latin-1', 'replace').decode('latin-1')
        self.multi_cell(0, 5, safe_body)
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
            return {
                "statusCode": 400, "headers": CORS_HEADERS,
                "body": json.dumps({"error": "jobDescription and fileId are required"})
            }

        # RAG Pipeline
        query_embedding = genai.embed_content(model="models/text-embedding-004", content=job_description, task_type="RETRIEVAL_QUERY")['embedding']
        query_response = index.query(vector=query_embedding, top_k=5, include_metadata=True, filter={"original_file_id": {"$eq": file_id}})
        if not query_response['matches']:
             raise ValueError("Could not find any relevant sections in the master resume for this job description.")
        
        context_chunks = [match['metadata']['text'] for match in query_response['matches']]
        resume_context = "\n---\n".join(context_chunks)
        
        prompt = f"""
        You are a professional resume and cover letter writing assistant. Your task is to generate a tailored resume and a cover letter for a specific job application.
        You MUST ONLY use information provided in the 'MASTER RESUME CONTEXT' section. Do not invent, embellish, or infer any skills or experiences.

        **JOB DESCRIPTION:**
        ---
        {job_description}
        ---

        **MASTER RESUME CONTEXT:**
        ---
        {resume_context}
        ---

        **TASK:**
        1.  Generate a **Tailored Resume**: Review the JOB DESCRIPTION and select the most relevant experiences and skills from the MASTER RESUME CONTEXT. Format them as a professional resume in plain text. Prioritize accomplishments and skills that directly match the job requirements.
        2.  Generate a **Cover Letter**: Write a concise, professional cover letter in plain text. In the letter, highlight 2-3 key experiences from the MASTER RESUME CONTEXT that make the candidate a strong fit for the role described in the JOB DESCRIPTION.

        Provide the output in a single, valid JSON object with two keys: "tailoredResume" and "coverLetter". Do not add any extra text or formatting like ```json.
        """
        
        response = generative_model.generate_content(prompt)
        
        if not response.text:
            print(f"Gemini response was empty. Feedback: {response.prompt_feedback}")
            raise ValueError("The response from the AI was blocked or empty. This may be due to safety filters.")

        cleaned_response_text = response.text.strip().replace('```json', '').replace('```', '')
        generated_docs = json.loads(cleaned_response_text)

        # --- CORRECTED PDF Generation ---
        pdf = PDF()
        pdf.add_page()
        pdf.chapter_title('Cover Letter')
        pdf.chapter_body(generated_docs['coverLetter'])
        
        pdf.add_page()
        pdf.chapter_title('Tailored Resume')
        pdf.chapter_body(generated_docs['tailoredResume'])
        
        
        pdf_output_string = pdf.output(dest='S')
        pdf_output_bytes = pdf_output_string.encode('latin-1')

        pdf_base64 = base64.b64encode(pdf_output_bytes).decode('utf-8')
        
        return {
            "statusCode": 200, "headers": CORS_HEADERS,
            "body": pdf_base64, "isBase64Encoded": True
        }

    except Exception as e:
        print(f"Error generating documents: {e}")
        return {
            "statusCode": 500, "headers": CORS_HEADERS,
            "body": json.dumps({"error": f"Failed to generate documents: {str(e)}"})
        }