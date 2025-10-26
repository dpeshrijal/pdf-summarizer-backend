import json
import boto3
import google.generativeai as genai
from pinecone import Pinecone

# =================================================================
# Initialize Clients (done once per cold start)
# =================================================================
ssm = boto3.client('ssm')

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
        1.  Generate a **Tailored Resume**: Review the JOB DESCRIPTION and select the most relevant experiences and skills from the MASTER RESUME CONTEXT. Format them as a professional resume in plain text following this exact structure:
            - First line: Candidate's full name (from context)
            - Second line: Email: [email] | Git: [github] | LinkedIn: [linkedin]
            - Then sections in ALL CAPS like: SUMMARY, SKILLS, WORK EXPERIENCE, CERTIFICATION, EDUCATION
            - Under SKILLS, use subsection headers like "Programming Languages:", "Frameworks and Libraries:", etc. (bold these)
            - Under WORK EXPERIENCE, format as: "Job Title, Company Name (Location) (Start Year – End Year)" (bold this line)
            - Use bullet points (•) for achievements and responsibilities
            - Keep the format clean and professional
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

        # Return text response immediately without PDFs to avoid API Gateway timeout
        # User will get text content instantly
        print(f"Returning text response (without PDFs to avoid timeout)")
        response = {
            "statusCode": 200,
            "headers": CORS_HEADERS,
            "body": json.dumps(final_json_output)
        }
        print(f"Response status: {response['statusCode']}")
        return response

    except Exception as e:
        print(f"Error generating documents: {e}")
        return {
            "statusCode": 500,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": f"Failed to generate documents: {str(e)}"})
        }