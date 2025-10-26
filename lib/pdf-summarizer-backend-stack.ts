import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import { S3EventSource } from 'aws-cdk-lib/aws-lambda-event-sources';
import { PythonFunction } from '@aws-cdk/aws-lambda-python-alpha';

export class PdfSummarizerBackendStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // 1. Define the S3 Bucket for PDF uploads
    const uploadsBucket = new s3.Bucket(this, 'UploadsBucket', {
      cors: [
        {
          allowedMethods: [s3.HttpMethods.GET, s3.HttpMethods.POST, s3.HttpMethods.PUT],
          allowedOrigins: ['http://localhost:3000', 'https://pdf-summarizer-frontend-five.vercel.app'], 
          allowedHeaders: ['*'],
        },
      ],
      removalPolicy: cdk.RemovalPolicy.DESTROY, 
      autoDeleteObjects: true,
    });

    // 2. Define the DynamoDB Tables
    const summariesTable = new dynamodb.Table(this, 'SummariesTable', {
      partitionKey: { name: 'fileId', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // New table for generation jobs (async processing)
    const generationJobsTable = new dynamodb.Table(this, 'GenerationJobsTable', {
      partitionKey: { name: 'jobId', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      timeToLiveAttribute: 'ttl', // Auto-delete old jobs after 24 hours
    });
    
    // 3. Define the shared IAM Role for our Lambda functions
    const lambdaRole = new iam.Role(this, 'LambdaExecutionRole', {
        assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
        managedPolicies: [
            iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
        ],
    });

    // Grant necessary permissions to the role
    uploadsBucket.grantReadWrite(lambdaRole);
    summariesTable.grantReadWriteData(lambdaRole);
    generationJobsTable.grantReadWriteData(lambdaRole);
    lambdaRole.addToPolicy(new iam.PolicyStatement({
      actions: ['ssm:GetParameter'],
      resources: [
        `arn:aws:ssm:${this.region}:${this.account}:parameter/pdf-summarizer/gemini-api-key`,
        `arn:aws:ssm:${this.region}:${this.account}:parameter/pdf-summarizer/pinecone-api-key`,
        `arn:aws:ssm:${this.region}:${this.account}:parameter/pdf-summarizer/pinecone-environment`,
      ],
    }));
    // Allow Lambda to invoke other Lambda functions (for async invocation)
    lambdaRole.addToPolicy(new iam.PolicyStatement({
      actions: ['lambda:InvokeFunction'],
      resources: [`arn:aws:lambda:${this.region}:${this.account}:function:*`],
    }));

    // 4. Define the Lambda Functions using the automated PythonFunction construct
    const processPdfLambda = new PythonFunction(this, 'ProcessPdfLambda', {
        runtime: lambda.Runtime.PYTHON_3_12,
        entry: 'lambda/processPdf',      // Points to the folder with requirements.txt
        index: 'lambda_function.py',   // The file to use
        handler: 'lambda_handler',       // The function to call
        role: lambdaRole,
        timeout: cdk.Duration.seconds(45),
        memorySize: 512,
        environment: {
            TABLE_NAME: summariesTable.tableName,
        }
    });

    // Add the S3 trigger to the processPdfLambda
    processPdfLambda.addEventSource(new S3EventSource(uploadsBucket, {
        events: [s3.EventType.OBJECT_CREATED],
        filters: [{ suffix: '.pdf' }],
    }));

    const getSignedUrlLambda = new PythonFunction(this, 'GetSignedUrlLambda', {
        runtime: lambda.Runtime.PYTHON_3_12,
        entry: 'lambda/getSignedUploadUrl',
        index: 'lambda_function.py',
        handler: 'lambda_handler',
        role: lambdaRole,
        environment: {
            BUCKET_NAME: uploadsBucket.bucketName,
            TABLE_NAME: summariesTable.tableName,
        }
    });

    const getSummaryStatusLambda = new PythonFunction(this, 'GetSummaryStatusLambda', {
        runtime: lambda.Runtime.PYTHON_3_12,
        entry: 'lambda/getSummaryStatus',
        index: 'lambda_function.py',
        handler: 'lambda_handler',
        role: lambdaRole,
        environment: {
            TABLE_NAME: summariesTable.tableName,
        }
    });

    // New: Start Generation Lambda (returns immediately with jobId)
    const startGenerationLambda = new PythonFunction(this, 'StartGenerationLambda', {
    runtime: lambda.Runtime.PYTHON_3_12,
    entry: 'lambda/startGeneration',
    index: 'lambda_function.py',
    handler: 'lambda_handler',
    role: lambdaRole,
    timeout: cdk.Duration.seconds(15),
    memorySize: 256,
    environment: {
        GENERATION_JOBS_TABLE: generationJobsTable.tableName,
        PROCESS_GENERATION_FUNCTION_NAME: 'ProcessGenerationLambda', // Will be updated after creation
    }
});

    // Modified: Process Generation Lambda (async background processing)
    const processGenerationLambda = new PythonFunction(this, 'ProcessGenerationLambda', {
    runtime: lambda.Runtime.PYTHON_3_12,
    entry: 'lambda/processGeneration',
    index: 'lambda_function.py',
    handler: 'lambda_handler',
    role: lambdaRole,
    timeout: cdk.Duration.minutes(10), // 10 minutes for slow models
    memorySize: 512,
    environment: {
        BUCKET_NAME: uploadsBucket.bucketName,
        GENERATION_JOBS_TABLE: generationJobsTable.tableName,
        MODEL_NAME: 'gemini-2.5-flash', // Can be changed to gemini-2.5-pro
    }
});

    // Update startGeneration with the actual function name
    startGenerationLambda.addEnvironment('PROCESS_GENERATION_FUNCTION_NAME', processGenerationLambda.functionName);

    // New: Get Generation Status Lambda (for polling)
    const getGenerationStatusLambda = new PythonFunction(this, 'GetGenerationStatusLambda', {
    runtime: lambda.Runtime.PYTHON_3_12,
    entry: 'lambda/getGenerationStatus',
    index: 'lambda_function.py',
    handler: 'lambda_handler',
    role: lambdaRole,
    timeout: cdk.Duration.seconds(10),
    memorySize: 256,
    environment: {
        GENERATION_JOBS_TABLE: generationJobsTable.tableName,
    }
});

    // 5. Define the API Gateway
    const api = new apigateway.RestApi(this, 'PdfSummarizerApi', {
      defaultCorsPreflightOptions: {
        allowOrigins: apigateway.Cors.ALL_ORIGINS,
        allowMethods: apigateway.Cors.ALL_METHODS,
      }
    });

    // Create API endpoints and link them to our Lambdas
    api.root.resourceForPath('get-upload-url').addMethod('GET', new apigateway.LambdaIntegration(getSignedUrlLambda));
    api.root.resourceForPath('get-summary-status').addMethod('GET', new apigateway.LambdaIntegration(getSummaryStatusLambda));

    // New async generation endpoints
    api.root.resourceForPath('start-generation').addMethod('POST', new apigateway.LambdaIntegration(startGenerationLambda));
    api.root.resourceForPath('get-generation-status').addMethod('GET', new apigateway.LambdaIntegration(getGenerationStatusLambda));

    // 6. Output the new API URL
    new cdk.CfnOutput(this, 'ApiGatewayUrl', {
        value: api.url,
        description: 'The base URL for the API Gateway',
      });
  }
}