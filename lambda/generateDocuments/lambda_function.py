import json
import os
import boto3
import google.generativeai as genai
from pinecone import Pinecone
from fpdf import FPDF
from datetime import datetime, timedelta

# =================================================================
# Initialize Clients (done once per cold start)
# =================================================================
ssm = boto3.client('ssm')
s3 = boto3.client('s3')

# --- This is a standard header that will be included in all responses ---
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "OPTIONS,POST,GET"
}

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
    pc = Pinecone(api_key=pinecone_api_key) # Environment is often optional in newer client versions
    
    PINECONE_INDEX_NAME = "resume-embeddings" 
    index = pc.Index(PINECONE_INDEX_NAME)

    # Initialize the generative model for creating the content
    generative_model = genai.GenerativeModel('gemini-2.5-flash')

except Exception as e:
    print(f"FATAL: Could not initialize one or more services. Error: {e}")
    raise e

# =================================================================
# PDF Generation Helper Functions
# =================================================================
def create_pdf_from_text(text_content, title):
    """
    Create a PDF from plain text content using fpdf2.
    Returns the PDF content as bytes.
    """
    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    # Set margins to ensure enough space
    pdf.set_left_margin(20)
    pdf.set_right_margin(20)

    # Add title
    pdf.set_font("Arial", "B", 16)
    # Use encode with 'latin-1' and 'replace' to handle special characters
    safe_title = title.encode('latin-1', 'replace').decode('latin-1')
    pdf.cell(0, 10, safe_title, ln=True, align="C")
    pdf.ln(10)

    # Add content
    pdf.set_font("Arial", "", 11)

    # Split text into lines and add to PDF
    lines = text_content.split('\n')
    for line in lines:
        # Handle empty lines
        if not line.strip():
            pdf.ln(5)
        else:
            try:
                # Clean the line - remove or replace problematic characters
                # Convert to latin-1 compatible string
                safe_line = line.encode('latin-1', 'replace').decode('latin-1')
                # Use multi_cell to handle long lines with wrapping
                pdf.multi_cell(0, 6, safe_line)
            except Exception as e:
                # If still fails, skip the line and log
                print(f"Warning: Could not add line to PDF: {e}")
                continue

    return pdf.output(dest='S')  # Return as bytes

def upload_pdf_to_s3(pdf_content, bucket_name, s3_key):
    """
    Upload PDF content to S3 and return a presigned URL.
    """
    # Upload to S3
    s3.put_object(
        Bucket=bucket_name,
        Key=s3_key,
        Body=pdf_content,
        ContentType='application/pdf'
    )

    # Generate presigned URL (valid for 1 hour)
    presigned_url = s3.generate_presigned_url(
        'get_object',
        Params={
            'Bucket': bucket_name,
            'Key': s3_key
        },
        ExpiresIn=3600  # 1 hour
    )

    return presigned_url

# =================================================================
# Main Lambda Handler
# =================================================================
def lambda_handler(event, context):
    try:
        # API Gateway wraps the body in a string, so we need to parse it.
        body = json.loads(event.get('body', '{}'))
        
        job_description = body.get('jobDescription')
        file_id = body.get('fileId')

        if not job_description or not file_id:
            print("Error: jobDescription and fileId are required.")
            return {
                "statusCode": 400,
                "headers": CORS_HEADERS,
                "body": json.dumps({"error": "jobDescription and fileId are required"})
            }

        # 1. Create an embedding for the job description
        print("Creating embedding for job description...")
        query_embedding = genai.embed_content(
            model="models/text-embedding-004",
            content=job_description,
            task_type="RETRIEVAL_QUERY"
        )['embedding']

        # 2. Query Pinecone to get the most relevant resume chunks
        print("Querying Pinecone for relevant resume sections...")
        query_response = index.query(
            vector=query_embedding,
            top_k=5, # Get the top 5 most relevant sections
            include_metadata=True,
            # Filter by the original fileId to ensure we only get chunks from the correct resume
            filter={"original_file_id": {"$eq": file_id}}
        )
        
        if not query_response['matches']:
             raise ValueError("Could not find any relevant sections in the master resume for this job description.")

        context_chunks = [match['metadata']['text'] for match in query_response['matches']]
        resume_context = "\n---\n".join(context_chunks)
        print(f"Retrieved context for prompt.")

        # 3. Construct the detailed prompt for Gemini
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

        # 4. Call the Gemini API to generate the documents
        print("Generating documents with Gemini...")
        response = generative_model.generate_content(prompt)
        
        # Clean up the response from Gemini - it sometimes includes markdown formatting
        cleaned_response_text = response.text.strip().replace('```json', '').replace('```', '')
        
        # Ensure the response is valid JSON before sending it back
        final_json_output = json.loads(cleaned_response_text)

        print("Successfully generated documents.")

        # Generate PDFs from the text content (optional - if it fails, users still get text)
        try:
            print("Generating PDF files...")
            resume_pdf = create_pdf_from_text(
                final_json_output['tailoredResume'],
                "Tailored Resume"
            )
            cover_letter_pdf = create_pdf_from_text(
                final_json_output['coverLetter'],
                "Cover Letter"
            )

            # Get bucket name from environment variable
            bucket_name = os.environ.get('BUCKET_NAME')
            if not bucket_name:
                print("Warning: BUCKET_NAME environment variable not set, skipping PDF upload")
            else:
                # Upload PDFs to S3 and get presigned URLs
                print("Uploading PDFs to S3...")
                resume_s3_key = f"generated/{file_id}-resume.pdf"
                cover_letter_s3_key = f"generated/{file_id}-cover-letter.pdf"

                resume_pdf_url = upload_pdf_to_s3(resume_pdf, bucket_name, resume_s3_key)
                cover_letter_pdf_url = upload_pdf_to_s3(cover_letter_pdf, bucket_name, cover_letter_s3_key)

                print("PDFs uploaded successfully.")

                # Add PDF URLs to the response
                final_json_output['resumePdfUrl'] = resume_pdf_url
                final_json_output['coverLetterPdfUrl'] = cover_letter_pdf_url

        except Exception as pdf_error:
            print(f"Warning: PDF generation failed, but text content is available. Error: {pdf_error}")
            # Continue without PDFs - user still gets text content

        return {
            "statusCode": 200,
            "headers": CORS_HEADERS,
            "body": json.dumps(final_json_output) # Re-serialize the cleaned JSON
        }

    except Exception as e:
        print(f"Error generating documents: {e}")
        return {
            "statusCode": 500,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": f"Failed to generate documents: {str(e)}"})
        }