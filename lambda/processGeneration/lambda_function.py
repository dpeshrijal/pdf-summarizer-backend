import json
import os
import boto3
import google.generativeai as genai
from pinecone import Pinecone

# =================================================================
# Initialize Clients (done once per cold start)
# =================================================================
ssm = boto3.client('ssm')
dynamodb = boto3.resource('dynamodb')

# Environment variables
GENERATION_JOBS_TABLE = os.environ.get('GENERATION_JOBS_TABLE')
MODEL_NAME = os.environ.get('MODEL_NAME', 'gemini-2.5-pro')

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
    pc = Pinecone(api_key=pinecone_api_key)

    PINECONE_INDEX_NAME = "resume-embeddings"
    index = pc.Index(PINECONE_INDEX_NAME)

    # Initialize the generative model (can be changed via environment variable)
    print(f"Initializing model: {MODEL_NAME}")
    generative_model = genai.GenerativeModel(MODEL_NAME)

except Exception as e:
    print(f"FATAL: Could not initialize one or more services. Error: {e}")
    raise e

# =================================================================
# Main Lambda Handler
# =================================================================
def lambda_handler(event, context):
    """
    Processes document generation in the background.
    Updates DynamoDB with status and results.
    """
    job_id = None

    try:
        # Extract parameters from event (async invocation)
        job_id = event.get('jobId')
        job_description = event.get('jobDescription')
        file_id = event.get('fileId')

        if not job_id or not job_description or not file_id:
            raise ValueError("jobId, jobDescription, and fileId are required")

        print(f"Processing generation job: {job_id} with model: {MODEL_NAME}")

        # Get DynamoDB table
        table = dynamodb.Table(GENERATION_JOBS_TABLE)

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
            top_k=5,
            include_metadata=True,
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
        1.  Generate a **Tailored Resume**: Review the JOB DESCRIPTION and select the most relevant experiences and skills from the MASTER RESUME CONTEXT.

            Format as a professional ATS-friendly resume following this EXACT structure:

            **LINE 1:** [Candidate's Full Name]
            **LINE 2:** Email: [email] | Git: github.com/username | LinkedIn: linkedin.com/in/username

            **SUMMARY**
            [2-3 sentences highlighting key qualifications relevant to the job]

            **SKILLS**
            Programming Languages: [list]
            Frameworks & Libraries: [list]
            Databases: [list]
            Cloud & DevOps: [list]
            Tools: [list]

            **WORK EXPERIENCE**
            Job Title, Company Name (Location) (Start Year - End Year or Present)
            • [Achievement/responsibility using action verbs - quantify when possible]
            • [Achievement/responsibility using action verbs - quantify when possible]
            • [Achievement/responsibility using action verbs - quantify when possible]

            IMPORTANT: The date (Start Year - End Year) should be at the END of the job title line in parentheses.
            Example: Software Developer, Request Finance (Paris, France) (2022 - Present)

            [Repeat for each relevant position]

            **CERTIFICATIONS**
            • [Certification Name] ([Year])
            [Only include if certifications exist in the master resume]

            **EDUCATION**
            [Degree Name]
            [Institution Name], [Location] ([Start Year] - [End Year])
            Relevant Coursework: [list if applicable]

            IMPORTANT FORMATTING RULES:
            - Section headers (SUMMARY, SKILLS, etc.) must be in ALL CAPS
            - Job titles should include company, location, and dates in format: "Title, Company (Location) (YYYY - YYYY)"
            - Use bullet points (•) for all achievements and responsibilities
            - Use action verbs (Developed, Led, Implemented, Optimized, etc.)
            - Be specific and quantify achievements when possible
            - Only include information from the MASTER RESUME CONTEXT - do not fabricate details

        2.  Generate a **Cover Letter**: Write a professional cover letter in business letter format. Include:
            - Opening paragraph expressing interest in the specific role
            - 2-3 paragraphs highlighting relevant experiences from MASTER RESUME CONTEXT that match the job requirements
            - Closing paragraph with call to action
            - Keep it concise (under 400 words)

        Provide the output in a single, valid JSON object with two keys: "tailoredResume" and "coverLetter". Do not add any extra text or formatting like ```json.
        """

        # 4. Call the Gemini API to generate the documents
        print(f"Generating documents with {MODEL_NAME}...")
        response = generative_model.generate_content(prompt)

        # Clean up the response from Gemini
        cleaned_response_text = response.text.strip().replace('```json', '').replace('```', '')

        # Parse JSON
        final_json_output = json.loads(cleaned_response_text)

        print("Successfully generated documents.")

        # 5. Update DynamoDB with COMPLETED status and results
        table.update_item(
            Key={'jobId': job_id},
            UpdateExpression='SET #status = :status, tailoredResume = :resume, coverLetter = :coverLetter, completedAt = :completedAt',
            ExpressionAttributeNames={
                '#status': 'status'
            },
            ExpressionAttributeValues={
                ':status': 'COMPLETED',
                ':resume': final_json_output['tailoredResume'],
                ':coverLetter': final_json_output['coverLetter'],
                ':completedAt': int(context.get_remaining_time_in_millis() / 1000) if context else 0
            }
        )

        print(f"Job {job_id} completed successfully")
        return {"statusCode": 200, "message": "Generation completed"}

    except Exception as e:
        print(f"Error processing generation job {job_id}: {e}")

        # Update DynamoDB with FAILED status
        if job_id:
            try:
                table = dynamodb.Table(GENERATION_JOBS_TABLE)
                table.update_item(
                    Key={'jobId': job_id},
                    UpdateExpression='SET #status = :status, errorMessage = :error',
                    ExpressionAttributeNames={
                        '#status': 'status'
                    },
                    ExpressionAttributeValues={
                        ':status': 'FAILED',
                        ':error': str(e)
                    }
                )
            except Exception as update_error:
                print(f"Failed to update DynamoDB with error status: {update_error}")

        raise e
