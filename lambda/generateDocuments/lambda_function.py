import json
import os
import boto3
import google.generativeai as genai
from pinecone import Pinecone

# =================================================================
# Initialize Clients (done once per cold start)
# =================================================================
ssm = boto3.client('ssm')

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
        print("Received event:", json.dumps(event))
        body = json.loads(event.get('body', '{}'))
        
        job_description = body.get('jobDescription')
        file_id = body.get('fileId')

        if not job_description or not file_id:
            return {
                "statusCode": 400,
                "headers": {"Access-Control-Allow-Origin": "*", "Content-Type": "application/json"},
                "body": json.dumps({"error": "jobDescription and fileId are required"})
            }

        # 1. Create an embedding for the job description to find relevant context
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
            include_metadata=True
        )
        
        context_chunks = [match['metadata']['text'] for match in query_response['matches']]
        resume_context = "\n---\n".join(context_chunks)
        print(f"Retrieved context: {resume_context}")

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
        1.  Generate a **Tailored Resume**: Review the JOB DESCRIPTION and select the most relevant experiences and skills from the MASTER RESUME CONTEXT. Format them as a professional resume. Prioritize accomplishments and skills that directly match the job requirements.
        2.  Generate a **Cover Letter**: Write a concise, professional cover letter. In the letter, highlight 2-3 key experiences from the MASTER RESUME CONTEXT that make the candidate a strong fit for the role described in the JOB DESCRIPTION.

        Provide the output in a single JSON object with two keys: "tailoredResume" and "coverLetter".
        """

        # 4. Call the Gemini API to generate the documents
        print("Generating documents with Gemini...")
        response = generative_model.generate_content(prompt)
        
        # Clean up the response from Gemini - it often includes ```json ... ```
        cleaned_response = response.text.strip().replace('```json', '').replace('```', '')
        
        print("Successfully generated documents.")
        
        return {
            "statusCode": 200,
            "headers": {"Access-Control-Allow-Origin": "*", "Content-Type": "application/json"},
            "body": cleaned_response
        }

    except Exception as e:
        print(f"Error generating documents: {e}")
        return {
            "statusCode": 500,
            "headers": {"Access-Control-Allow-Origin": "*", "Content-Type": "application/json"},
            "body": json.dumps({"error": f"Failed to generate documents: {str(e)}"})
        }