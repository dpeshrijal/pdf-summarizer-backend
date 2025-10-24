import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import { S3EventSource } from 'aws-cdk-lib/aws-lambda-event-sources';
import * as ssm from 'aws-cdk-lib/aws-ssm';

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

    // 2. Define the DynamoDB Table to store summaries
    const summariesTable = new dynamodb.Table(this, 'SummariesTable', {
      partitionKey: { name: 'fileId', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    
    // 3. Define the IAM Role for our Lambda functions
    const lambdaRole = new iam.Role(this, 'LambdaExecutionRole', {
        assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
        managedPolicies: [
            iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
        ],
    });

    // Add specific permissions to the role
    uploadsBucket.grantReadWrite(lambdaRole);
    summariesTable.grantReadWriteData(lambdaRole);
    
    // Grant permission to read the Gemini API key from SSM Parameter Store
    // THIS BLOCK IS THE ONLY CHANGE
    lambdaRole.addToPolicy(new iam.PolicyStatement({
      actions: ['ssm:GetParameter'],
      resources: [`arn:aws:ssm:${this.region}:${this.account}:parameter/pdf-summarizer/gemini-api-key`],
    }));


    // 4. Define the Lambda Layers
    const pyMuPDFLayer = lambda.LayerVersion.fromLayerVersionArn(this, 'PyMuPDFLayer', 'arn:aws:lambda:us-east-1:770693421928:layer:Klayers-p312-PyMuPDF:2'); 
    const geminiLayer = lambda.LayerVersion.fromLayerVersionArn(this, 'GeminiLayer', 'arn:aws:lambda:us-east-1:828244906528:layer:GeminiLayer-Compatible:1'); 


    // 5. Define the Lambda Functions, pointing to our local code
    const processPdfLambda = new lambda.Function(this, 'ProcessPdfLambda', {
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: 'lambda_function.lambda_handler',
        code: lambda.Code.fromAsset('lambda/processPdf'),
        role: lambdaRole,
        timeout: cdk.Duration.seconds(30),
        memorySize: 512,
        layers: [pyMuPDFLayer, geminiLayer],
        environment: {
            TABLE_NAME: summariesTable.tableName,
        }
    });

    // Add the S3 trigger to the processPdfLambda
    processPdfLambda.addEventSource(new S3EventSource(uploadsBucket, {
        events: [s3.EventType.OBJECT_CREATED],
        filters: [{ suffix: '.pdf' }],
    }));

    const getSignedUrlLambda = new lambda.Function(this, 'GetSignedUrlLambda', {
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: 'lambda_function.lambda_handler',
        code: lambda.Code.fromAsset('lambda/getSignedUploadUrl'),
        role: lambdaRole,
        environment: {
            BUCKET_NAME: uploadsBucket.bucketName,
            TABLE_NAME: summariesTable.tableName,
        }
    });

    const getSummaryStatusLambda = new lambda.Function(this, 'GetSummaryStatusLambda', {
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: 'lambda_function.lambda_handler',
        code: lambda.Code.fromAsset('lambda/getSummaryStatus'),
        role: lambdaRole,
        environment: {
            TABLE_NAME: summariesTable.tableName,
        }
    });

    // 6. Define the API Gateway to trigger our functions
    const api = new apigateway.RestApi(this, 'PdfSummarizerApi', {
      defaultCorsPreflightOptions: {
        allowOrigins: apigateway.Cors.ALL_ORIGINS,
        allowMethods: apigateway.Cors.ALL_METHODS,
      }
    });

    // Create API endpoints and link them to our Lambdas
    api.root.resourceForPath('get-upload-url').addMethod('GET', new apigateway.LambdaIntegration(getSignedUrlLambda));
    api.root.resourceForPath('get-summary-status').addMethod('GET', new apigateway.LambdaIntegration(getSummaryStatusLambda));

    // 7. Add a CfnOutput to easily find the new API URL after deployment
    new cdk.CfnOutput(this, 'ApiGatewayUrl', {
        value: api.url,
        description: 'The base URL for the API Gateway',
      });
  }
}