import json
import os
import time
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

        # Get userId from summaries table for security filtering
        summaries_table = dynamodb.Table(os.environ.get('SUMMARIES_TABLE'))
        file_record = summaries_table.get_item(Key={'fileId': file_id})

        if 'Item' not in file_record or 'userId' not in file_record['Item']:
            raise ValueError(f"Could not find userId for fileId: {file_id}")

        user_id = file_record['Item']['userId']
        print(f"Retrieved userId: {user_id} for fileId: {file_id}")

        # 1. Extract company name from job description using Gemini
        print("Extracting company name from job description...")
        company_name = "Unknown Company"
        try:
            extraction_prompt = f"""Extract ONLY the company name from this job description.
Return just the company name, nothing else. If you cannot find a company name, return "Unknown Company".

Job Description:
{job_description[:1000]}"""  # Only use first 1000 chars for speed

            extraction_response = generative_model.generate_content(
                extraction_prompt,
                generation_config={
                    "temperature": 0.1,  # Low temperature for factual extraction
                    "max_output_tokens": 50,
                }
            )
            company_name = extraction_response.text.strip()
            print(f"Extracted company name: {company_name}")
        except Exception as e:
            print(f"Error extracting company name: {e}")
            company_name = "Unknown Company"

        # 2. Create an embedding for the job description
        print("Creating embedding for job description...")
        query_embedding = genai.embed_content(
            model="models/text-embedding-004",
            content=job_description,
            task_type="RETRIEVAL_QUERY"
        )['embedding']

        # 2. Query Pinecone to get the most relevant resume chunks with retry logic
        # IMPORTANT: Filter by BOTH fileId AND userId for security
        print("Querying Pinecone for relevant resume sections...")

        max_retries = 2  # Try twice total (initial + 1 retry)
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
                    print(f"Successfully found {len(query_response['matches'])} matches on attempt {attempt + 1}")
                    break  # Success! Exit retry loop
                else:
                    print(f"No matches found on attempt {attempt + 1}")
                    if attempt < max_retries - 1:
                        print("Retrying Pinecone query...")
                        time.sleep(1)  # Brief pause before retry
            except Exception as e:
                print(f"Error during Pinecone query attempt {attempt + 1}: {str(e)}")
                if attempt < max_retries - 1:
                    print("Retrying after error...")
                    time.sleep(1)
                else:
                    raise

        if not query_response or not query_response['matches']:
            raise ValueError("Could not find any relevant sections in the master resume for this job description after retrying.")

        context_chunks = [match['metadata']['text'] for match in query_response['matches']]
        resume_context = "\n---\n".join(context_chunks)
        print(f"Retrieved context for prompt.")

        # 3. Construct the detailed prompt for Gemini
        prompt = f"""
        You are an elite resume strategist and career advisor with expertise in ATS optimization and human psychology. Your mission: Generate a resume and cover letter that BOTH passes automated screening AND compels hiring managers to call this candidate immediately.

        **JOB DESCRIPTION:**
        ---
        {job_description}
        ---

        **MASTER RESUME CONTEXT:**
        ---
        {resume_context}
        ---

        **DUAL OPTIMIZATION STRATEGY:**
        This resume must win twice:
        1. **BEAT THE ATS** - Match keywords, use standard formatting, mirror job terminology
        2. **WOW THE HUMAN** - Sound natural, tell a compelling story, avoid AI/template language

        **VOICE & STYLE (CRITICAL FOR HUMAN READERS):**
        - Write like a confident professional, not a template or AI. Vary sentence structure.
        - Use concrete, specific language. Ban these clichés: "results-driven," "dynamic," "passionate," "seasoned professional," "team player," "think outside the box"
        - Eliminate filler words: "successfully," "very," "highly," "effectively," "efficiently" (unless genuinely needed)
        - Active voice only. Strong, varied action verbs. Never repeat the same verb opener in consecutive bullets.
        - Tense consistency: past roles = past tense; current role = present tense
        - For resumes: No first person ("I," "my"). For cover letters: First person is fine but stay professional.

        **ATS OPTIMIZATION (CRITICAL FOR GETTING THROUGH):**
        - Use EXACT terminology from job description for: technical skills, tools, certifications, methodologies
        - Mirror key phrases from job requirements in your bullets, but integrate naturally into strong sentences
        - Prioritize job description keywords in SKILLS section (use their exact spelling/capitalization)
        - Section headers MUST be standard: SUMMARY, SKILLS, WORK EXPERIENCE, CERTIFICATIONS, EDUCATION
        - Job titles and company names must be prominent and consistently formatted
        - Don't sacrifice readability for keywords—modern ATS penalizes keyword stuffing

        **STRATEGIC SKILL EXTRACTION (The Secret Sauce):**
        Extract implicit skills from explicit experience—this is honest and powerful:

        **Tech/Software:**
        - "used AWS Lambda" → serverless architecture, cloud-native development, event-driven systems, infrastructure as code
        - "debugged production issues" → troubleshooting, root cause analysis, performance optimization, monitoring
        - "reviewed code" → code quality standards, best practices, mentorship, architectural patterns

        **Healthcare/Pharmacy:**
        - "administered medications" → medication management, drug interactions, patient safety protocols, pharmacotherapy
        - "counseled patients" → patient education, health literacy, medication adherence, therapeutic communication
        - "worked with EHR systems" → healthcare IT, clinical workflows, data accuracy, regulatory compliance

        **Business/Finance:**
        - "managed projects" → project management, stakeholder coordination, resource allocation, risk mitigation
        - "prepared financial reports" → financial analysis, data visualization, forecasting, business intelligence
        - "led meetings" → cross-functional leadership, communication, conflict resolution, decision-making

        **Education:**
        - "taught courses" → curriculum development, instructional design, learning assessment, differentiated instruction
        - "managed classroom" → classroom management, student engagement, behavioral strategies, inclusive practices
        - "adapted lessons" → personalized learning, student-centered teaching, formative assessment, creative problem-solving

        **Marketing/Sales:**
        - "ran campaigns" → campaign strategy, audience targeting, A/B testing, ROI optimization, funnel analysis
        - "created content" → content strategy, brand voice, storytelling, audience engagement, SEO principles
        - "analyzed metrics" → data-driven marketing, performance analytics, conversion optimization, KPI tracking

        **Universal Extraction Rules:**
        ✅ Extract what they genuinely learned or did
        ✅ Use industry-standard terminology for these skills
        ✅ Connect related competencies within their field
        ❌ Don't add technologies they never touched
        ❌ Don't invent responsibilities that didn't exist

        **STRICT INTEGRITY RULES (NON-NEGOTIABLE):**
        - NEVER add tools, technologies, or certifications not mentioned in resume context
        - NEVER invent job responsibilities, projects, or achievements
        - NEVER fabricate dates, companies, or credentials
        - NEVER inflate years of experience with specific technologies
        - Use reasonable estimates for quantification, but stay grounded in context
        - If unsure whether something is true, DON'T include it

        **TASK 1 — TAILORED RESUME (ONE PAGE ONLY, ~500–650 words):**
        - Be selective: 2–3 roles, recent 5–7 years unless older is crucial.
        - Max 3–4 bullets per role; one sentence per bullet.
        - Summary 2–3 sentences; Skills ~80–100 words in 4–6 clear categories.
        - Use numbers, scope, and outcomes. Avoid generic claims.

        Produce the resume in this EXACT structure and formatting:

        **LINE 1:** [Candidate's Full Name]
        **LINE 2:** Email: [email] | Git: github.com/username | LinkedIn: linkedin.com/in/username

        **SUMMARY**
        [2-3 concise sentences, ~50-60 words total, focusing on the most job-relevant strengths. No clichés.]

        **SKILLS**
        [4-6 concise categories, ~80-100 words total. Use job terminology only where natural. Group related items.]

        **ADAPT CATEGORIES TO FIELD:**
        For Tech/Software roles:
        Programming Languages: [list]
        Frameworks & Tools: [list]
        Cloud & DevOps: [list]

        For Healthcare/Pharmacy roles:
        Clinical Skills: [list]
        Medications & Therapies: [list]
        Systems & Software: [list]

        For Business/Finance roles:
        Technical Skills: [list]
        Business Tools: [list]
        Analytical Skills: [list]

        For Education roles:
        Teaching Methods: [list]
        Subject Expertise: [list]
        Educational Technology: [list]

        For Marketing/Sales roles:
        Digital Marketing: [list]
        Analytics & Tools: [list]
        Campaign Management: [list]

        For Other Professions:
        Use field-appropriate groupings (e.g., Technical Skills, Industry Knowledge, Tools & Systems).

        **WORK EXPERIENCE**
        [2–3 most relevant roles, ~250–300 words total]

        [SINGLE Most Relevant Job Title], Company Name (Location) (Start Year - End Year or Present)
        • [3–4 bullets max per role; ONE sentence each]
        • [Lead with the most job-relevant accomplishments first]
        • [Each bullet must have: Action Verb + What You Did + Quantified Impact (when possible)]

        **CRITICAL JOB TITLE RULE:**
        - Pick ONE SINGLE job title that is most relevant to the target role
        - If the candidate held multiple titles at the same company, choose the most senior or most relevant one
        - NEVER combine multiple titles with slashes (e.g., "Developer / Researcher")
        - Keep titles concise and industry-standard (e.g., "Full Stack Developer" NOT "Full Stack Developer / Undergraduate Researcher")
        - The goal is clarity and professionalism - hiring managers should instantly understand the role

        **DATE FORMATTING RULE:**
        - Use "Mon YYYY" format for dates (e.g., "Jan 2021", "May 2022", "Present")
        - Month abbreviations: Jan, Feb, Mar, Apr, May, Jun, Jul, Aug, Sep, Oct, Nov, Dec
        - NEVER use all caps (e.g., "MAY 2021" is WRONG, use "May 2021")
        - Example: "Full Stack Developer, Google (Jan 2021 - May 2023)"

        **BULLET WRITING FORMULA (HIGH-IMPACT):**
        [Strong Verb] + [Specific Action with JD Keywords] + [Quantified Outcome/Scope]

        Examples showing PERFECT vs WEAK bullets:

        Tech/Software:
        ✅ "Architected serverless ETL pipeline using AWS Lambda and DynamoDB, reducing infrastructure costs 40% while processing 50K+ daily transactions"
        ✅ "Led migration of monolithic app to microservices on Kubernetes, improving deployment speed 10x and eliminating 90% of downtime"
        ❌ "Worked on AWS projects using Lambda and DynamoDB to reduce costs"
        ❌ "Successfully implemented cloud solutions"

        Healthcare/Pharmacy:
        ✅ "Counseled 50+ patients daily on medication management and drug interactions, improving adherence rates 35% through personalized education"
        ✅ "Collaborated with physicians to optimize medication therapy for 200+ chronic disease patients, reducing adverse events by 28%"
        ❌ "Talked to patients about medications"
        ❌ "Provided excellent patient care"

        Business/Finance:
        ✅ "Led cross-functional cost reduction initiative across 3 departments, delivering $2M in annual savings—20% above target"
        ✅ "Built financial forecasting models in Excel and Tableau that improved budget accuracy by 45% and informed $10M in strategic decisions"
        ❌ "Worked on cost-saving projects"
        ❌ "Managed budgets and financial reports"

        Education:
        ✅ "Designed and delivered differentiated curriculum for 120+ students across 5 courses, raising average test scores 25% over two semesters"
        ✅ "Implemented blended learning approach using Canvas LMS and adaptive tech, increasing student engagement metrics 40%"
        ❌ "Taught classes and created lesson plans"
        ❌ "Helped students learn better"

        Marketing/Sales:
        ✅ "Launched multi-channel digital campaign (email, social, paid search) reaching 200K+ prospects and generating $500K in qualified pipeline"
        ✅ "Optimized conversion funnel through A/B testing and analytics, boosting lead-to-customer rate from 8% to 14% in 6 months"
        ❌ "Ran marketing campaigns"
        ❌ "Increased sales and engagement"

        **ACTION VERBS BY IMPACT (Use variety—never repeat):**
        Leadership: Led, Directed, Managed, Coordinated, Spearheaded, Orchestrated
        Creation: Built, Developed, Designed, Architected, Created, Established, Launched
        Improvement: Optimized, Enhanced, Streamlined, Improved, Transformed, Modernized
        Analysis: Analyzed, Assessed, Evaluated, Diagnosed, Identified, Investigated
        Collaboration: Partnered, Collaborated, Coordinated, Facilitated, Aligned
        Achievement: Delivered, Achieved, Exceeded, Generated, Drove, Produced

        **CERTIFICATIONS**
        [Include only if real and relevant; 1–2 lines]
        • [Certification Name] ([Year])

        **EDUCATION**
        [~40–50 words, 2–3 lines; coursework only if highly relevant]
        [Degree Name]
        [Institution Name], [Location] ([Start Year] - [End Year])

        **FORMATTING & CONSISTENCY RULES:**
        - Section headers in ALL CAPS.
        - Title line format exactly: "Title, Company (Location) (YYYY - YYYY)".
        - Bullets start with • and remain one sentence.
        - No dense keyword lists; write for readability and clarity.

        **ONE-PAGE CHECK (~500–650 words):**
        - Header: 2 lines
        - Summary: ~50–60 words
        - Skills: ~80–100 words
        - Experience: ~250–300 words
        - Certifications (if any): ~10–20 words
        - Education: ~40–50 words
        If over limit: cut older/less relevant content and tighten bullets.

        **TASK 2 — COVER LETTER (300–400 words):**
        **Mission:** Make the hiring manager think "I need to talk to this person" within 30 seconds.

        **STRUCTURE (4 paragraphs):**

        **Paragraph 1 - Opening (2-3 sentences):**
        - Name the specific role and where you saw it
        - Give ONE concrete reason this opportunity interests you (reference something from the JD: company mission, specific project, technology, impact)
        - Avoid: "I am writing to express my interest..." or "I am excited to apply..."
        - Instead: Start with confidence and specificity

        Example openings:
        ✅ "I'm applying for the Senior Software Engineer role on your Platform team. The opportunity to build scalable infrastructure supporting millions of users while working with modern cloud technologies aligns perfectly with my 5 years architecting high-traffic systems."
        ✅ "Your Clinical Pharmacist opening caught my attention because of [Company]'s focus on medication therapy management and patient outcomes—work I've been passionate about throughout my 7 years in hospital pharmacy."
        ❌ "I am writing to express my enthusiastic interest in this amazing opportunity at your innovative company."

        **Paragraph 2 - Proof (3-4 sentences):**
        - Pick the TOP 2 requirements from job description
        - Give specific examples showing you've done this work (use numbers, technologies, scope)
        - Mirror JD terminology but keep sentences natural
        - This paragraph wins or loses the interview

        Example:
        ✅ "In my current role, I've led the migration of our monolithic application to microservices on Kubernetes, reducing deployment time from hours to minutes while eliminating 90% of production incidents. I also architected our serverless data pipeline using AWS Lambda and DynamoDB, which now processes 50K+ transactions daily at 40% lower cost than our previous infrastructure."
        ❌ "I have extensive experience with cloud technologies and have successfully delivered many projects using best practices and innovative solutions."

        **Paragraph 3 - Fit & Approach (2-3 sentences):**
        - Show how you work: collaboration, learning, ownership (pull language from JD's "ideal candidate" or "you are" sections)
        - One brief, authentic insight about why this environment appeals to you
        - Keep it grounded—no fluff about "passion" or "dream job"

        Example:
        ✅ "I work best in cross-functional environments where I can partner with product and design to solve complex problems. Your mention of 'bias toward action' and 'iterative development' resonates—I've found quick feedback loops and data-driven iteration lead to better outcomes than lengthy planning cycles."
        ❌ "I am a passionate team player who loves to think outside the box and bring innovative solutions to challenging problems."

        **Paragraph 4 - Closing (2 sentences):**
        - Brief, confident call to action
        - Express genuine interest in discussing further
        - NO clichés: avoid "thrilled," "passionate," "dream opportunity," "honored"

        Example:
        ✅ "I'd welcome the chance to discuss how my experience with [specific tech/skill from JD] could contribute to [specific goal/project mentioned in JD]. I'm available for a conversation at your convenience."
        ❌ "I would be absolutely thrilled and honored to have the opportunity to bring my passion and expertise to your amazing team. I look forward to hearing from you soon!"

        **COVER LETTER TONE RULES (CRITICAL):**
        - Confident but humble. Specific but concise.
        - Use first person ("I," "my"), but don't make every sentence about "I"
        - Vary sentence structure: mix short and medium sentences
        - ONE vivid example beats five generic claims
        - If you can't be specific, don't write it
        - Ban these phrases: "I am writing to," "I believe that," "I am confident that," "proven track record," "hit the ground running," "wear many hats"

        **ATS NOTE FOR COVER LETTER:**
        - Include 3-5 key terms from job description naturally in your examples
        - Role title should appear at least once
        - 2-3 technical skills/tools from requirements should be mentioned
        - Keep format simple: no tables, no fancy formatting

        Provide the output in a single, valid JSON object with two keys: "tailoredResume" and "coverLetter". Do not add any extra text or formatting like ```json.
        """

        # 4. Call the Gemini API to generate the documents with optimized settings
        print(f"Generating documents with {MODEL_NAME}...")
        generation_config = genai.GenerationConfig(
            temperature=0.7,  # Balance between consistency and creativity
            top_p=0.9,       # Nucleus sampling for quality
            top_k=40,        # Limit token selection for coherence
        )
        response = generative_model.generate_content(
            prompt,
            generation_config=generation_config
        )

        # Clean up the response from Gemini
        cleaned_response_text = response.text.strip().replace('```json', '').replace('```', '')

        # Parse JSON
        final_json_output = json.loads(cleaned_response_text)

        # Validate required keys exist
        if 'tailoredResume' not in final_json_output or 'coverLetter' not in final_json_output:
            raise ValueError("Generated output missing required keys (tailoredResume or coverLetter)")

        # Validate content is not empty
        if not final_json_output['tailoredResume'].strip() or not final_json_output['coverLetter'].strip():
            raise ValueError("Generated output contains empty documents")

        print("Successfully generated documents.")

        # 5. Update DynamoDB with COMPLETED status and results
        table.update_item(
            Key={'jobId': job_id},
            UpdateExpression='SET #status = :status, tailoredResume = :resume, coverLetter = :coverLetter, completedAt = :completedAt, companyName = :companyName, #ttl = :ttl',
            ExpressionAttributeNames={
                '#status': 'status',
                '#ttl': 'ttl'
            },
            ExpressionAttributeValues={
                ':status': 'COMPLETED',
                ':resume': final_json_output['tailoredResume'],
                ':coverLetter': final_json_output['coverLetter'],
                ':completedAt': int(time.time()),
                ':companyName': company_name,
                ':ttl': int(time.time()) + (365 * 24 * 60 * 60)  # Keep for 1 year instead of 24 hours
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
