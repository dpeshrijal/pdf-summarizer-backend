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
MODEL_NAME = os.environ.get('MODEL_NAME', 'gemini-2.5-pro')  # Main model for resume generation

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

    # Validate contact information (only name and email are required)
    contact = resume['contact']
    required_contact_fields = ['name', 'email']
    for field in required_contact_fields:
        if field not in contact or not contact[field]:
            errors.append(f"Contact missing required '{field}' field")

    # Phone, linkedin, github, location are all optional - just ensure they're strings if present
    optional_contact_fields = ['phone', 'linkedin', 'github', 'location']
    for field in optional_contact_fields:
        if field in contact and contact[field] is not None and not isinstance(contact[field], str):
            errors.append(f"Contact '{field}' must be a string if provided")

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

    # Validate optional sections (all are optional, but if present must be valid)

    # Projects (optional)
    if 'projects' in resume:
        if not isinstance(resume['projects'], list):
            errors.append("'projects' must be an array")
        else:
            for idx, proj in enumerate(resume['projects']):
                if 'name' not in proj or 'description' not in proj:
                    errors.append(f"Project {idx} missing 'name' or 'description' field")

    # Publications (optional)
    if 'publications' in resume:
        if not isinstance(resume['publications'], list):
            errors.append("'publications' must be an array")
        else:
            for idx, pub in enumerate(resume['publications']):
                required_pub_fields = ['title', 'authors', 'venue', 'date']
                for field in required_pub_fields:
                    if field not in pub:
                        errors.append(f"Publication {idx} missing '{field}' field")

    # Certifications (optional)
    if 'certifications' in resume:
        if not isinstance(resume['certifications'], list):
            errors.append("'certifications' must be an array")
        else:
            for idx, cert in enumerate(resume['certifications']):
                required_cert_fields = ['name', 'issuer']
                for field in required_cert_fields:
                    if field not in cert:
                        errors.append(f"Certification {idx} missing '{field}' field")

    # Awards (optional)
    if 'awards' in resume:
        if not isinstance(resume['awards'], list):
            errors.append("'awards' must be an array")
        else:
            for idx, award in enumerate(resume['awards']):
                required_award_fields = ['title', 'issuer', 'date']
                for field in required_award_fields:
                    if field not in award:
                        errors.append(f"Award {idx} missing '{field}' field")

    # Volunteer Experience (optional)
    if 'volunteerExperience' in resume:
        if not isinstance(resume['volunteerExperience'], list):
            errors.append("'volunteerExperience' must be an array")
        else:
            for idx, vol in enumerate(resume['volunteerExperience']):
                required_vol_fields = ['role', 'organization', 'startDate', 'endDate', 'description']
                for field in required_vol_fields:
                    if field not in vol:
                        errors.append(f"Volunteer {idx} missing '{field}' field")

    # Professional Memberships (optional)
    if 'professionalMemberships' in resume:
        if not isinstance(resume['professionalMemberships'], list):
            errors.append("'professionalMemberships' must be an array")
        else:
            for idx, memb in enumerate(resume['professionalMemberships']):
                if 'organization' not in memb:
                    errors.append(f"Membership {idx} missing 'organization' field")

    # Languages (optional)
    if 'languages' in resume:
        if not isinstance(resume['languages'], list):
            errors.append("'languages' must be an array")
        else:
            for idx, lang in enumerate(resume['languages']):
                if 'language' not in lang or 'proficiency' not in lang:
                    errors.append(f"Language {idx} missing 'language' or 'proficiency' field")

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

    # Validate match score structure
    if 'matchScore' not in data:
        errors.append("Missing 'matchScore' field")
    else:
        match_score = data['matchScore']
        required_score_fields = ['overallScore', 'skillsMatch', 'experienceMatch', 'educationMatch', 'summary', 'strengths', 'gaps']
        for field in required_score_fields:
            if field not in match_score:
                errors.append(f"Match score missing '{field}' field")

        # Validate score ranges (0-100)
        score_fields = ['overallScore', 'skillsMatch', 'experienceMatch', 'educationMatch']
        for field in score_fields:
            if field in match_score:
                score = match_score[field]
                if not isinstance(score, (int, float)) or score < 0 or score > 100:
                    errors.append(f"Match score '{field}' must be a number between 0-100")

        # Validate arrays
        if 'strengths' in match_score and not isinstance(match_score['strengths'], list):
            errors.append("Match score 'strengths' must be an array")
        if 'gaps' in match_score and not isinstance(match_score['gaps'], list):
            errors.append("Match score 'gaps' must be an array")

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

        lite_model = genai.GenerativeModel('gemini-2.5-flash-lite')
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

        # Fetch user profile (if exists) for contact info
        profile_data = None
        try:
            profiles_table = dynamodb.Table(os.environ.get('USER_PROFILES_TABLE'))
            profile_response = profiles_table.get_item(Key={'userId': user_id})
            if 'Item' in profile_response:
                profile_data = profile_response['Item']
                print(f"Found user profile for userId: {user_id}")
            else:
                print(f"No profile found for userId: {user_id}, will extract from resume")
        except Exception as e:
            print(f"Warning: Could not fetch user profile: {str(e)}")

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
    "projects": [
      {{
        "name": "string (project name)",
        "description": "string (1-2 sentences about the project)",
        "technologies": ["tech1", "tech2"] or null,
        "url": "string or null (GitHub, live demo, etc.)",
        "date": "string or null (format: 'Mon YYYY' or 'YYYY')"
      }}
    ],
    "publications": [
      {{
        "title": "string (paper/article title)",
        "authors": "string (all authors)",
        "venue": "string (journal, conference, book)",
        "date": "string (year or 'Mon YYYY')",
        "url": "string or null",
        "doi": "string or null"
      }}
    ],
    "certifications": [
      {{
        "name": "string (certification name)",
        "issuer": "string (issuing organization)",
        "date": "string or null (format: 'Mon YYYY' e.g. 'Jan 2021')",
        "expiryDate": "string or null (if applicable)",
        "credentialId": "string or null (if available)"
      }}
    ],
    "awards": [
      {{
        "title": "string (award name)",
        "issuer": "string (organization)",
        "date": "string (format: 'Mon YYYY' or 'YYYY')",
        "description": "string or null (brief description)"
      }}
    ],
    "education": [
      {{
        "degree": "string",
        "institution": "string",
        "location": "string or null",
        "graduationYear": "string",
        "coursework": ["Course 1", "Course 2"] or null (only if relevant courses mentioned in master resume)
      }}
    ],
    "volunteerExperience": [
      {{
        "role": "string",
        "organization": "string",
        "location": "string or null",
        "startDate": "string (format: 'Mon YYYY')",
        "endDate": "string (format: 'Mon YYYY' or 'Present')",
        "description": ["achievement1", "achievement2"]
      }}
    ],
    "professionalMemberships": [
      {{
        "organization": "string (e.g., 'IEEE', 'American Medical Association')",
        "role": "string or null (e.g., 'Member', 'Board Member')",
        "startDate": "string or null (format: 'Mon YYYY' or 'YYYY')",
        "endDate": "string or null (format: 'Mon YYYY' or 'Present')"
      }}
    ],
    "languages": [
      {{
        "language": "string",
        "proficiency": "string (Native, Fluent, Professional, Conversational, Basic)"
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
  }},
  "matchScore": {{
    "overallScore": 85 (integer 0-100, overall match percentage),
    "skillsMatch": 90 (integer 0-100, how well skills align),
    "experienceMatch": 80 (integer 0-100, how relevant experience is),
    "educationMatch": 85 (integer 0-100, education requirements match),
    "summary": "string (2-3 sentences explaining the match score)",
    "strengths": [
      "string (key strength 1: what matches well)",
      "string (key strength 2)",
      "string (key strength 3)"
    ],
    "gaps": [
      "string (gap 1: what's missing or weak)",
      "string (gap 2)"
    ]
  }}
}}
```

**STRICT RULES (NON-NEGOTIABLE):**

1. **NO FABRICATION**: Only use information from the master resume. Never invent:
   - Contact details (LinkedIn, GitHub, location)
   - Technologies, tools, or skills not mentioned
   - Job titles, companies, or dates
   - Certifications, licenses, or degrees
   - Projects, publications, awards, volunteer work, memberships, or languages
   - If a section is not in the master resume, omit the entire section (leave it as empty array or don't include it)

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
   - **MINIMUM 4-5 achievements per role** (use more if only 1-2 jobs total to maximize page usage)
   - If candidate has only 1-2 work experiences, include 5-6 achievements each to fully utilize space
   - Each achievement: Action verb + What + Quantified impact
   - Order achievements by relevance to job description
   - Expand on responsibilities and accomplishments to showcase full scope of work

6. **PROJECTS** (if present in master resume - common for developers, designers, students):
   - Include 2-4 most relevant projects
   - Brief description (1-2 sentences)
   - Technologies used (if applicable)
   - Link to GitHub/demo/portfolio if available

7. **PUBLICATIONS** (if present - for academics, researchers, thought leaders):
   - Include most relevant publications (papers, articles, books)
   - Format: Title, Authors, Venue (journal/conference), Date
   - Include DOI or URL if available

8. **CERTIFICATIONS** (if present in master resume):
   - Include relevant certifications, licenses, or professional credentials
   - Only include if explicitly mentioned in master resume
   - Format: Certification name, Issuing organization, Date (if available)
   - Examples: AWS Certified Solutions Architect, PMP, RN License, CPA, CFA, etc.

9. **AWARDS & HONORS** (if present - shows recognition and excellence):
   - Include relevant awards, honors, scholarships, recognitions
   - Format: Award name, Issuing organization, Date
   - Examples: Dean's List, Employee of the Year, Hackathon Winner, etc.

10. **EDUCATION**:
   - Include all degrees from master resume
   - Format: Degree name, Institution, Location (if available), Year
   - **Include relevant coursework** if mentioned in master resume (especially useful for students/recent grads)
   - Select 3-5 most relevant courses to the job description
   - Helps fill space while showcasing relevant knowledge

11. **VOLUNTEER EXPERIENCE** (if present - shows character and community involvement):
   - Include significant volunteer roles
   - Format similar to work experience with achievements
   - Shows leadership, compassion, community engagement

12. **PROFESSIONAL MEMBERSHIPS** (if present - shows active engagement in field):
   - Include relevant professional organizations
   - Examples: IEEE, AMA, Bar Association, PMI, etc.
   - Include role if more than just "Member"

13. **LANGUAGES** (if present - valuable for many roles):
   - List spoken languages with proficiency level
   - Format: Language, Proficiency (Native, Fluent, Professional, Conversational, Basic)
   - Only include if mentioned in master resume

14. **COVER LETTER**:
   - 4 paragraphs (opening, proof, fit, closing)
   - Opening: Mention specific role and one reason for interest
   - Proof: Top 2 job requirements with specific examples
   - Fit: How you work and why this environment appeals
   - Closing: Call to action, express interest in discussing
   - Natural, conversational tone
   - Use first person but vary sentence structure

15. **MATCH SCORE** (ATS Compatibility Analysis):
   - **IMPORTANT**: Calculate scores with precision - use specific numbers (e.g., 73, 67, 88) NOT rounded numbers (avoid patterns like 70, 75, 80, 85, 90)
   - **Overall Score (0-100)**: Comprehensive match percentage based on weighted average
     * Calculate by: (skillsMatch * 0.4) + (experienceMatch * 0.35) + (educationMatch * 0.25)
     * 90-100: Excellent match, highly qualified
     * 75-89: Strong match, well qualified
     * 60-74: Good match, qualified with some gaps
     * 40-59: Moderate match, missing key requirements
     * 0-39: Weak match, significant gaps
   - **Skills Match (0-100)**: Compare candidate's skills vs job requirements
     * Count total required skills in job posting
     * Count how many candidate possesses
     * Calculate: (matched_skills / total_required_skills) * 100
     * Consider skill level (basic vs expert) and adjust accordingly
     * Be precise - use actual calculation, not estimates
   - **Experience Match (0-100)**: Years of experience and role relevance
     * Compare years: candidate's experience vs required years
     * Evaluate role relevance: how closely past roles align
     * Calculate based on both factors with specific reasoning
   - **Education Match (0-100)**: Education requirements alignment
     * Degree level match (Bachelor's, Master's, PhD)
     * Field of study relevance
     * Certifications and specialized training
     * Calculate based on requirements fulfillment
   - **Summary**: 2-3 sentences explaining the score objectively
   - **Strengths**: 3-5 specific areas where candidate excels for this role
   - **Gaps**: 1-3 areas where candidate may be weaker or missing requirements
     * Be honest but constructive
     * If score >85%, gaps can be minor or "none identified"
   - **Use realistic, varied scores** - avoid numbers ending in 0 or 5 unless genuinely accurate

**JOB DESCRIPTION:**
---
{job_description}
---

{"**USER PROFILE (Priority Contact Info):**" if profile_data else ""}
{f"""---
Name: {profile_data.get('name')}
Email: {profile_data.get('email')}
Phone: {profile_data.get('phone', 'Not provided')}
Location: {profile_data.get('location', 'Not provided')}
LinkedIn: {profile_data.get('linkedinUrl', 'Not provided')}
GitHub: {profile_data.get('githubUrl', 'Not provided')}
Portfolio: {profile_data.get('portfolioUrl', 'Not provided')}
{f"Custom Link ({profile_data.get('customUrlLabel')}): {profile_data.get('customUrl')}" if profile_data.get('customUrl') else ''}
---

**IMPORTANT**: Use the above profile contact information as the PRIMARY source for contact details. Only fall back to master resume if profile data is incomplete.
""" if profile_data else ""}
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
