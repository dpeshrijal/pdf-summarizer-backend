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

    // 2. Define the DynamoDB Table
    const summariesTable = new dynamodb.Table(this, 'SummariesTable', {
      partitionKey: { name: 'fileId', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
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
    lambdaRole.addToPolicy(new iam.PolicyStatement({
      actions: ['ssm:GetParameter'],
      resources: [
        `arn:aws:ssm:${this.region}:${this.account}:parameter/pdf-summarizer/gemini-api-key`,
        `arn:aws:ssm:${this.region}:${this.account}:parameter/pdf-summarizer/pinecone-api-key`,
        `arn:aws:ssm:${this.region}:${this.account}:parameter/pdf-summarizer/pinecone-environment`,
      ],
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

    const generateDocumentsLambda = new PythonFunction(this, 'GenerateDocumentsLambda', {
    runtime: lambda.Runtime.PYTHON_3_12,
    entry: 'lambda/generateDocuments', 
    index: 'lambda_function.py',
    handler: 'lambda_handler',
    role: lambdaRole, 
    timeout: cdk.Duration.seconds(60),
    memorySize: 512,
});

    // 5. Define the API Gateway
    const api = new apigateway.RestApi(this, 'PdfSummarizerApi', {
      defaultCorsPreflightOptions: {
        allowOrigins: apigateway.Cors.ALL_ORIGINS,
        allowMethods: apigateway.Cors.ALL_METHODS,
      },
      // Enable binary media types for PDF responses
      binaryMediaTypes: ['application/pdf', '*/*']
    });

    // Create API endpoints and link them to our Lambdas
    api.root.resourceForPath('get-upload-url').addMethod('GET', new apigateway.LambdaIntegration(getSignedUrlLambda));
    api.root.resourceForPath('get-summary-status').addMethod('GET', new apigateway.LambdaIntegration(getSummaryStatusLambda));


    const generateDocumentsResource = api.root.addResource('generate-documents');
    generateDocumentsResource.addMethod('POST', new apigateway.LambdaIntegration(generateDocumentsLambda, {
      contentHandling: apigateway.ContentHandling.CONVERT_TO_BINARY,
    }));

    // 6. Output the new API URL
    new cdk.CfnOutput(this, 'ApiGatewayUrl', {
        value: api.url,
        description: 'The base URL for the API Gateway',
      });
  }
}