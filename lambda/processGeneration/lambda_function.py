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
        You are an expert resume strategist and career advisor. Your task is to generate a highly competitive, ATS-optimized resume and cover letter that maximizes the candidate's chances of getting interviews.

        **JOB DESCRIPTION:**
        ---
        {job_description}
        ---

        **MASTER RESUME CONTEXT:**
        ---
        {resume_context}
        ---

        **STRATEGIC TAILORING PHILOSOPHY:**
        Your goal is to present the candidate in the STRONGEST possible light by:
        1. Extracting implicit skills and expertise from their actual experience
        2. Using strategic language that matches the job description's terminology
        3. Highlighting transferable skills and relevant accomplishments
        4. Positioning the candidate as an ideal fit through honest reframing

        **CRITICAL RULES - FOLLOW EXACTLY:**

        ✅ **DO (Strategic Optimization):**
        - Extract implicit skills from explicit experience (e.g., "used AWS Lambda" → implies serverless architecture, event-driven design, cloud-native development)
        - Match job description terminology exactly (if job says "full-stack" and candidate has both, say "full-stack")
        - Expand abbreviations and technical depth from actual experience
        - Use strong action verbs that match job requirements (Led, Architected, Optimized, Delivered, Spearheaded)
        - Quantify achievements wherever possible (even estimates like "improved performance" → "optimized performance by ~30%")
        - Reframe responsibilities to highlight impact and alignment with job needs
        - Connect related technologies (Docker experience → containerization expertise, deployment automation)
        - Emphasize learning agility and adaptability when there are skill gaps

        ❌ **DO NOT (Fabrication):**
        - Add technologies or tools the candidate has never used
        - Invent job responsibilities that didn't exist
        - Claim certifications not mentioned
        - Create fictional projects or companies
        - Add years of experience with technologies they haven't used

        **TASK:**
        1.  Generate a **Tailored Resume**: Strategically present the candidate's experience to maximize alignment with the job description.

            **CRITICAL CONSTRAINT: The resume MUST fit on ONE PAGE (approximately 500-650 words total)**

            To achieve this:
            - Be concise and impactful - every word must add value
            - Limit work experience to 3-4 bullet points per job
            - Focus on most recent/relevant positions (last 5-7 years)
            - Limit skills section to most relevant technologies
            - Summary should be 2-3 sentences max
            - Omit less relevant older positions if space is tight
            - Prioritize quality over quantity

            Format as a professional ATS-friendly resume following this EXACT structure:

            **LINE 1:** [Candidate's Full Name]
            **LINE 2:** Email: [email] | Git: github.com/username | LinkedIn: linkedin.com/in/username

            **SUMMARY**
            [2-3 concise sentences, ~50-60 words total, highlighting key qualifications most relevant to job]

            **SKILLS**
            [Keep concise - 4-6 categories max, ~80-100 words total]
            - Prioritize skills mentioned in job description
            - Use job description terminology for categories
            - Group related technologies together
            - List only most relevant skills

            Example format (adjust categories based on job requirements):
            Programming Languages: [list 5-8 most relevant]
            Frameworks & Tools: [list 5-8 most relevant]
            Cloud & DevOps: [list 4-6 most relevant]
            Databases: [list 2-4 most relevant]

            Keep it scannable and focused - recruiters spend 6 seconds on first pass!

            **WORK EXPERIENCE**
            [Include 2-3 most relevant positions, ~250-300 words total]

            Job Title, Company Name (Location) (Start Year - End Year or Present)
            • [3-4 bullet points per position max]
            • [Each bullet should be 1-2 lines max]
            • [Focus on impact and results]

            STRATEGIC BULLET POINT WRITING (BE CONCISE!):
            • Lead with accomplishments that most closely match job requirements
            • Use strong action verbs: Architected, Spearheaded, Optimized, Delivered, Led, Engineered
            • Quantify impact when possible (use reasonable estimates)
            • Each bullet should be ONE powerful sentence (not multiple sentences)
            • Include technical keywords from job description

            Example (concise):
            ✅ "Architected serverless solutions using AWS Lambda and DynamoDB, reducing infrastructure costs by 40%"
            ❌ "Worked on AWS projects. Used Lambda for serverless functions. Also implemented DynamoDB. This helped reduce costs."

            IMPORTANT:
            - Date format: "Job Title, Company Name (Location) (YYYY - YYYY)"
            - Only include last 5-7 years of experience (unless older roles are highly relevant)
            - If candidate has 5+ roles, include only top 2-3 most relevant

            **CERTIFICATIONS**
            [Only include if certifications exist AND are relevant to job. Keep to 1-2 lines max]
            • [Certification Name] ([Year])

            **EDUCATION**
            [Keep brief - 2-3 lines max, ~40-50 words]
            [Degree Name]
            [Institution Name], [Location] ([Start Year] - [End Year])
            [Only include coursework if HIGHLY relevant to job and space permits]

            IMPORTANT FORMATTING RULES:
            - Section headers (SUMMARY, SKILLS, etc.) must be in ALL CAPS
            - Job titles should include company, location, and dates in format: "Title, Company (Location) (YYYY - YYYY)"
            - Use bullet points (•) for all achievements and responsibilities
            - Use powerful action verbs that match job description language
            - Be specific and quantify achievements (use reasonable estimates if exact numbers unknown)
            - Mirror job description keywords naturally throughout the resume
            - Make every word count - this is a competitive application

            **STRATEGIC MINDSET:**
            Think like a hiring manager reading this resume against the job description. Your goal is to make them think:
            "This candidate is exactly what we're looking for!" while being 100% truthful.

            Extract maximum value from their actual experience:
            - If they deployed code → they understand deployment pipelines, DevOps practices
            - If they used a framework → they understand the ecosystem, best practices, architecture patterns
            - If they solved a problem → they're a problem-solver who can handle similar challenges
            - If they worked in a team → they have collaboration, communication, and teamwork skills
            - If they delivered features → they have end-to-end ownership and delivery capabilities

            HONESTY BOUNDARY:
            - Extract and expand on implicit skills from actual work ✅
            - Use strategic language and framing ✅
            - Quantify with reasonable estimates ✅
            - Add technologies they've never touched ❌
            - Invent projects or experiences ❌
            - Falsify dates or companies ❌

            **FINAL CHECK - ONE PAGE REQUIREMENT:**
            Before finalizing, verify the resume is approximately 500-650 words total:
            - Header: 2 lines
            - Summary: 50-60 words (2-3 sentences)
            - Skills: 80-100 words (4-6 categories)
            - Work Experience: 250-300 words (2-3 positions, 3-4 bullets each)
            - Certifications: 10-20 words (if applicable)
            - Education: 40-50 words (2-3 lines)

            If over word count: Prioritize most recent/relevant experience, remove older positions, shorten bullet points.
            Remember: 1 page is NON-NEGOTIABLE for most positions under 10 years experience.

        2.  Generate a **Cover Letter**: Write a compelling, personalized cover letter that makes the hiring manager want to interview this candidate.

            COVER LETTER STRATEGY:
            - Opening: Express genuine enthusiasm for the role and company, mention the specific position
            - Body paragraph 1: Highlight 2-3 key experiences that directly align with the most important job requirements
            - Body paragraph 2: Demonstrate cultural fit and why you're excited about this opportunity (based on job description language)
            - Body paragraph 3: Show understanding of the company's challenges/goals (infer from job description) and how you can contribute
            - Closing: Strong call to action expressing eagerness to discuss how you can contribute

            STRATEGIC APPROACH:
            - Mirror language from job description (if they say "innovative," use "innovation")
            - Show you understand their needs and have solved similar problems
            - Be confident but not arrogant
            - Make it about THEM (what you can do for the company) not just YOU
            - Keep it concise (300-400 words)
            - Use specific examples from actual experience that match their needs

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
