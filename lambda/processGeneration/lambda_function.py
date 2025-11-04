import json
import os
import time
import boto3
import google.generativeai as genai
from pinecone import Pinecone
from decimal import Decimal

# =================================================================
# Initialize Clients (done once per cold start)
# =================================================================
ssm = boto3.client('ssm')
dynamodb = boto3.resource('dynamodb')

# Environment variables
GENERATION_JOBS_TABLE = os.environ.get('GENERATION_JOBS_TABLE')
MODEL_NAME = os.environ.get('MODEL_NAME', 'gemini-2.5-flash')  # Changed to Flash for faster JSON generation

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

    # Initialize the generative model
    print(f"Initializing model: {MODEL_NAME}")
    generative_model = genai.GenerativeModel(MODEL_NAME)

except Exception as e:
    print(f"FATAL: Could not initialize one or more services. Error: {e}")
    raise e

# =================================================================
# Validation Functions
# =================================================================

def validate_structured_output(data):
    """
    Comprehensive validation of structured output.
    Ensures all required fields are present and properly formatted.
    """
    errors = []

    # Check top-level structure
    if not isinstance(data, dict):
        raise ValueError("Output must be a JSON object")

    if 'resume' not in data:
        errors.append("Missing 'resume' field")
    if 'coverLetter' not in data:
        errors.append("Missing 'coverLetter' field")

    if errors:
        raise ValueError("; ".join(errors))

    resume = data['resume']
    cover_letter = data['coverLetter']

    # Validate resume structure
    required_resume_fields = ['contact', 'summary', 'skills', 'experience', 'education']
    for field in required_resume_fields:
        if field not in resume:
            errors.append(f"Resume missing '{field}' field")

    if errors:
        raise ValueError("; ".join(errors))

    # Validate contact information
    contact = resume['contact']
    required_contact_fields = ['name', 'email', 'phone']
    for field in required_contact_fields:
        if field not in contact or not contact[field]:
            errors.append(f"Contact missing '{field}' field")

    # Ensure arrays are actually arrays
    if not isinstance(resume['skills'], list):
        errors.append("'skills' must be an array")
    if not isinstance(resume['experience'], list):
        errors.append("'experience' must be an array")
    if not isinstance(resume['education'], list):
        errors.append("'education' must be an array")

    # Validate skill categories
    for idx, skill_cat in enumerate(resume['skills']):
        if not isinstance(skill_cat, dict):
            errors.append(f"Skill category {idx} must be an object")
        elif 'category' not in skill_cat or 'skills' not in skill_cat:
            errors.append(f"Skill category {idx} missing 'category' or 'skills' field")
        elif not isinstance(skill_cat['skills'], list):
            errors.append(f"Skill category {idx} 'skills' must be an array")

    # Validate experience entries
    for idx, exp in enumerate(resume['experience']):
        required_exp_fields = ['title', 'company', 'startDate', 'endDate', 'achievements']
        for field in required_exp_fields:
            if field not in exp:
                errors.append(f"Experience {idx} missing '{field}' field")
        if 'achievements' in exp and not isinstance(exp['achievements'], list):
            errors.append(f"Experience {idx} 'achievements' must be an array")

    # Validate education entries
    for idx, edu in enumerate(resume['education']):
        required_edu_fields = ['degree', 'institution', 'graduationYear']
        for field in required_edu_fields:
            if field not in edu:
                errors.append(f"Education {idx} missing '{field}' field")

    # Validate cover letter structure
    if not isinstance(cover_letter, dict):
        errors.append("Cover letter must be an object")
    else:
        required_cl_fields = ['companyName', 'position', 'paragraphs']
        for field in required_cl_fields:
            if field not in cover_letter:
                errors.append(f"Cover letter missing '{field}' field")
        if 'paragraphs' in cover_letter and not isinstance(cover_letter['paragraphs'], list):
            errors.append("Cover letter 'paragraphs' must be an array")

    if errors:
        raise ValueError("; ".join(errors))

    return True

def extract_company_and_position(job_description):
    """
    Extract company name and job position from job description.
    Uses Gemini Flash Lite for fast, cheap extraction.
    """
    try:
        extraction_prompt = f"""Extract the company name and job title from this job description.
Return ONLY valid JSON in this exact format:
{{"company": "Company Name", "position": "Job Title"}}

If you cannot find the information, use "Unknown Company" or "Unknown Position".

Job Description:
{job_description[:1500]}"""

        lite_model = genai.GenerativeModel('gemini-2.0-flash-lite')
        extraction_response = lite_model.generate_content(
            extraction_prompt,
            generation_config={
                "temperature": 0.1,
                "max_output_tokens": 100,
                "response_mime_type": "application/json"
            }
        )

        # Parse JSON response
        result = json.loads(extraction_response.text.strip())
        company_name = result.get('company', 'Unknown Company')
        job_title = result.get('position', 'Unknown Position')

        print(f"Extracted company: {company_name}, position: {job_title}")
        return company_name, job_title

    except Exception as e:
        print(f"Error extracting company/position: {e}")
        return "Unknown Company", "Unknown Position"

# =================================================================
# Main Lambda Handler
# =================================================================
def lambda_handler(event, context):
    """
    Processes document generation in the background.
    Returns structured JSON for consistent formatting.
    """
    job_id = None

    try:
        # Extract parameters
        job_id = event.get('jobId')
        job_description = event.get('jobDescription')
        file_id = event.get('fileId')

        if not job_id or not job_description or not file_id:
            raise ValueError("jobId, jobDescription, and fileId are required")

        print(f"Processing generation job: {job_id} with model: {MODEL_NAME}")

        # Get DynamoDB table
        table = dynamodb.Table(GENERATION_JOBS_TABLE)

        # Get userId from summaries table
        summaries_table = dynamodb.Table(os.environ.get('SUMMARIES_TABLE'))
        file_record = summaries_table.get_item(Key={'fileId': file_id})

        if 'Item' not in file_record or 'userId' not in file_record['Item']:
            raise ValueError(f"Could not find userId for fileId: {file_id}")

        user_id = file_record['Item']['userId']
        print(f"Retrieved userId: {user_id} for fileId: {file_id}")

        # Extract company name and job title
        company_name, job_title = extract_company_and_position(job_description)

        # Create embedding for job description
        print("Creating embedding for job description...")
        query_embedding = genai.embed_content(
            model="models/text-embedding-004",
            content=job_description,
            task_type="RETRIEVAL_QUERY"
        )['embedding']

        # Query Pinecone with retry logic
        print("Querying Pinecone for relevant resume sections...")
        max_retries = 2
        query_response = None

        for attempt in range(max_retries):
            try:
                print(f"Pinecone query attempt {attempt + 1}/{max_retries}...")
                query_response = index.query(
                    vector=query_embedding,
                    top_k=5,
                    include_metadata=True,
                    filter={
                        "$and": [
                            {"original_file_id": {"$eq": file_id}},
                            {"user_id": {"$eq": user_id}}
                        ]
                    }
                )

                if query_response['matches']:
                    print(f"Successfully found {len(query_response['matches'])} matches")
                    break
                else:
                    if attempt < max_retries - 1:
                        print("No matches found, retrying...")
                        time.sleep(1)
            except Exception as e:
                print(f"Error during Pinecone query: {str(e)}")
                if attempt < max_retries - 1:
                    time.sleep(1)
                else:
                    raise

        if not query_response or not query_response['matches']:
            raise ValueError("Could not find relevant sections in master resume")

        context_chunks = [match['metadata']['text'] for match in query_response['matches']]
        resume_context = "\n---\n".join(context_chunks)

        # Construct the structured JSON prompt
        prompt = f"""You are a professional resume optimization expert. You must generate a tailored resume and cover letter based on the master resume content and job description.

**CRITICAL: Return ONLY valid JSON. No markdown, no code blocks, no explanations.**

**OUTPUT SCHEMA (EXACT FORMAT REQUIRED):**
```json
{{
  "resume": {{
    "contact": {{
      "name": "string (EXACTLY as in master resume)",
      "email": "string (EXACTLY as in master resume)",
      "phone": "string (EXACTLY as in master resume)",
      "linkedin": "string or null (ONLY if present in master resume, do not fabricate)",
      "github": "string or null (ONLY if present in master resume, do not fabricate)",
      "location": "string or null (city, state if available)"
    }},
    "summary": "string (2-3 sentences, 50-70 words max, tailored to job description)",
    "skills": [
      {{
        "category": "string (e.g. 'Programming Languages', 'Clinical Skills', 'Business Tools')",
        "skills": ["skill1", "skill2", "skill3"]
      }}
    ],
    "experience": [
      {{
        "title": "string (ONE job title only, most relevant to target role)",
        "company": "string",
        "location": "string or null",
        "startDate": "string (format: 'Mon YYYY' e.g. 'Jan 2021')",
        "endDate": "string (format: 'Mon YYYY' or 'Present')",
        "achievements": [
          "string (one sentence, starts with action verb, includes metrics if possible)"
        ]
      }}
    ],
    "education": [
      {{
        "degree": "string",
        "institution": "string",
        "location": "string or null",
        "graduationYear": "string"
      }}
    ]
  }},
  "coverLetter": {{
    "companyName": "{company_name}",
    "position": "{job_title}",
    "paragraphs": [
      "string (opening paragraph)",
      "string (proof/experience paragraph)",
      "string (fit/approach paragraph)",
      "string (closing paragraph)"
    ]
  }}
}}
```

**STRICT RULES (NON-NEGOTIABLE):**

1. **NO FABRICATION**: Only use information from the master resume. Never invent:
   - Contact details (LinkedIn, GitHub, location)
   - Technologies, tools, or skills not mentioned
   - Job titles, companies, or dates
   - Certifications or degrees
   - If something is not in the master resume, use null or omit it

2. **CONTACT INFORMATION**:
   - Copy name, email, phone EXACTLY as they appear
   - Only include LinkedIn if it's in the master resume
   - Only include GitHub if it's in the master resume
   - Never create fake LinkedIn/GitHub URLs

3. **SUMMARY**:
   - 2-3 sentences max (50-70 words)
   - Focus on skills most relevant to job description
   - Use specific expertise areas, not generic claims
   - Mirror job description terminology naturally

4. **SKILLS**:
   - Group into 4-6 logical categories based on profession
   - Tech: "Programming Languages", "Frameworks & Tools", "Cloud & DevOps"
   - Healthcare: "Clinical Skills", "Medications & Therapies", "Systems & Software"
   - Business: "Technical Skills", "Business Tools", "Analytical Skills"
   - Only include skills actually mentioned or strongly implied in master resume

5. **EXPERIENCE**:
   - Select 2-3 most relevant roles
   - ONE job title per position (most relevant/senior)
   - Format dates: "Mon YYYY" (e.g. "Jan 2021", "Present")
   - 3-4 achievements per role
   - Each achievement: Action verb + What + Quantified impact
   - Order achievements by relevance to job description

6. **EDUCATION**:
   - Include all degrees from master resume
   - Format: Degree name, Institution, Location (if available), Year

7. **COVER LETTER**:
   - 4 paragraphs (opening, proof, fit, closing)
   - Opening: Mention specific role and one reason for interest
   - Proof: Top 2 job requirements with specific examples
   - Fit: How you work and why this environment appeals
   - Closing: Call to action, express interest in discussing
   - Natural, conversational tone
   - Use first person but vary sentence structure

**JOB DESCRIPTION:**
---
{job_description}
---

**MASTER RESUME CONTEXT:**
---
{resume_context}
---

Generate the structured JSON output now. Remember: NO markdown code blocks, NO explanations, ONLY the JSON object."""

        # Generate with strict JSON mode
        print(f"Generating structured output with {MODEL_NAME}...")
        generation_config = genai.GenerationConfig(
            temperature=0.7,
            top_p=0.9,
            top_k=40,
            response_mime_type="application/json"  # Force JSON output
        )

        response = generative_model.generate_content(
            prompt,
            generation_config=generation_config
        )

        # Clean and parse JSON
        cleaned_response = response.text.strip()

        # Remove markdown code blocks if present (extra safety)
        if cleaned_response.startswith('```json'):
            cleaned_response = cleaned_response.replace('```json', '').replace('```', '').strip()
        elif cleaned_response.startswith('```'):
            cleaned_response = cleaned_response.replace('```', '').strip()

        print(f"Raw response length: {len(cleaned_response)} characters")

        # Parse JSON
        structured_output = json.loads(cleaned_response)

        # Validate structure
        print("Validating structured output...")
        validate_structured_output(structured_output)

        print("✓ Validation passed!")

        # Convert to JSON string for storage
        structured_data_str = json.dumps(structured_output)

        # Update DynamoDB with structured data
        table.update_item(
            Key={'jobId': job_id},
            UpdateExpression='SET #status = :status, structuredData = :data, companyName = :companyName, jobTitle = :jobTitle, completedAt = :completedAt, #ttl = :ttl',
            ExpressionAttributeNames={
                '#status': 'status',
                '#ttl': 'ttl'
            },
            ExpressionAttributeValues={
                ':status': 'COMPLETED',
                ':data': structured_data_str,
                ':companyName': company_name,
                ':jobTitle': job_title,
                ':completedAt': int(time.time()),
                ':ttl': int(time.time()) + (365 * 24 * 60 * 60)  # 1 year retention
            }
        )

        print(f"✓ Job {job_id} completed successfully with structured output")
        return {"statusCode": 200, "message": "Generation completed"}

    except json.JSONDecodeError as e:
        error_msg = f"Failed to parse JSON from LLM: {str(e)}"
        print(f"❌ {error_msg}")

        if job_id:
            try:
                table = dynamodb.Table(GENERATION_JOBS_TABLE)
                table.update_item(
                    Key={'jobId': job_id},
                    UpdateExpression='SET #status = :status, errorMessage = :error',
                    ExpressionAttributeNames={'#status': 'status'},
                    ExpressionAttributeValues={
                        ':status': 'FAILED',
                        ':error': error_msg
                    }
                )
            except Exception as update_error:
                print(f"Failed to update error status: {update_error}")

        raise

    except ValueError as e:
        error_msg = f"Validation error: {str(e)}"
        print(f"❌ {error_msg}")

        if job_id:
            try:
                table = dynamodb.Table(GENERATION_JOBS_TABLE)
                table.update_item(
                    Key={'jobId': job_id},
                    UpdateExpression='SET #status = :status, errorMessage = :error',
                    ExpressionAttributeNames={'#status': 'status'},
                    ExpressionAttributeValues={
                        ':status': 'FAILED',
                        ':error': error_msg
                    }
                )
            except Exception as update_error:
                print(f"Failed to update error status: {update_error}")

        raise

    except Exception as e:
        error_msg = f"Unexpected error: {str(e)}"
        print(f"❌ {error_msg}")

        if job_id:
            try:
                table = dynamodb.Table(GENERATION_JOBS_TABLE)
                table.update_item(
                    Key={'jobId': job_id},
                    UpdateExpression='SET #status = :status, errorMessage = :error',
                    ExpressionAttributeNames={'#status': 'status'},
                    ExpressionAttributeValues={
                        ':status': 'FAILED',
                        ':error': error_msg
                    }
                )
            except Exception as update_error:
                print(f"Failed to update error status: {update_error}")

        raise
