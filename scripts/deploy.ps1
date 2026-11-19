<#
.SYNOPSIS
  Build and deploy the Finance Deep Search stack (Step Functions + Lambda + DynamoDB + S3).

.EXAMPLE
  .\scripts\deploy.ps1            # build zip, deploy, run a live research smoke test
  .\scripts\deploy.ps1 -Destroy   # delete everything this script created

  Reads .env (never printed): API_KEY, FINANCIAL_DATASETS_API_KEY, TAVILY_API_KEY, SEC_USER_AGENT,
  OPENAI_PROJECT_ID, optional BUDGET_EMAIL. Requires `aws login` and `py -3.13`.
#>
param(
  [string]$Region = "us-east-2",
  [string]$AppName = "finance-deep-search",
  [switch]$SkipSmoke,
  [switch]$Destroy
)
# "Continue": Windows PowerShell 5.1 turns native stderr into terminating errors under "Stop".
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Stack = "$AppName-stack"

function Read-DotEnv {
  $vals = @{}
  foreach ($line in Get-Content .env) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') { $vals[$Matches[1]] = $Matches[2].Trim('"').Trim("'") }
  }
  return $vals
}
function Invoke-Aws { & aws @args; if ($LASTEXITCODE -ne 0) { throw "aws $($args[0]) $($args[1]) failed" } }

$Account = (aws sts get-caller-identity --query Account --output text)
if ($LASTEXITCODE -ne 0) { throw "AWS CLI is not signed in. Run: aws login" }
$ArtifactBucket = "$AppName-artifacts-$Account-$Region"

if ($Destroy) {
  $runs = aws cloudformation describe-stacks --stack-name $Stack --region $Region --query "Stacks[0].Outputs[?OutputKey=='ArtifactsBucketName'].OutputValue" --output text 2>$null
  if ($runs -and $runs -ne "None") { aws s3 rm "s3://$runs" --recursive --region $Region | Out-Null }
  aws cloudformation delete-stack --stack-name $Stack --region $Region
  Write-Host "Waiting for stack deletion..."
  aws cloudformation wait stack-delete-complete --stack-name $Stack --region $Region
  aws s3 rb "s3://$ArtifactBucket" --force --region $Region 2>$null | Out-Null
  Write-Host "Deleted. SSM parameters under /$AppName/ kept (free); remove with:"
  Write-Host "  aws ssm delete-parameters --names /$AppName/api-key /$AppName/financial-datasets-api-key /$AppName/tavily-api-key /$AppName/sec-user-agent --region $Region"
  return
}

$envs = Read-DotEnv
foreach ($k in "API_KEY", "TAVILY_API_KEY", "SEC_USER_AGENT") { if (-not $envs[$k]) { throw "Set $k in .env" } }

Write-Host "[1/5] Storing secrets in SSM Parameter Store (SecureString)"
$secrets = @{ "api-key" = $envs["API_KEY"]; "tavily-api-key" = $envs["TAVILY_API_KEY"]; "sec-user-agent" = $envs["SEC_USER_AGENT"];
              "financial-datasets-api-key" = $(if ($envs["FINANCIAL_DATASETS_API_KEY"]) { $envs["FINANCIAL_DATASETS_API_KEY"] } else { "-" }) }
foreach ($name in $secrets.Keys) {
  Invoke-Aws ssm put-parameter --name "/$AppName/$name" --type SecureString --value $secrets[$name] --overwrite --region $Region | Out-Null
}

Write-Host "[2/5] Building Lambda zip (manylinux arm64 wheels)"
if (Test-Path build) { Remove-Item -Recurse -Force build }
New-Item -ItemType Directory build | Out-Null
& py -3.13 -m pip install -q -r requirements-lambda.txt --target build --upgrade `
    --platform manylinux2014_aarch64 --platform manylinux_2_28_aarch64 --implementation cp --python-version 3.13 --only-binary=:all:
if ($LASTEXITCODE -ne 0) { throw "pip install for Lambda failed" }
Copy-Item -Recurse deepsearch build/deepsearch
Get-ChildItem build -Recurse -Directory -Include __pycache__ | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
foreach ($d in "boto3", "botocore", "s3transfer") { Get-ChildItem build -Directory -Filter "$d*" | Remove-Item -Recurse -Force }
$sizeMb = [math]::Round(((Get-ChildItem build -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "      package size (unzipped): $sizeMb MB"

Write-Host "[3/5] Uploading package"
aws s3api head-bucket --bucket $ArtifactBucket --region $Region 2>$null
if ($LASTEXITCODE -ne 0) {
  Invoke-Aws s3api create-bucket --bucket $ArtifactBucket --region $Region --create-bucket-configuration "LocationConstraint=$Region" | Out-Null
}
Invoke-Aws s3api put-public-access-block --bucket $ArtifactBucket --region $Region `
    --public-access-block-configuration "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
Invoke-Aws cloudformation package --template-file template.yaml --s3-bucket $ArtifactBucket --s3-prefix $AppName `
    --output-template-file build-packaged.yaml --region $Region | Out-Null

Write-Host "[4/5] Deploying stack $Stack"
$params = @("AppName=$AppName", "BedrockProjectId=$($envs['OPENAI_PROJECT_ID'])")
if ($envs["BUDGET_EMAIL"]) { $params += "BudgetEmail=$($envs['BUDGET_EMAIL'])" }
Invoke-Aws cloudformation deploy --template-file build-packaged.yaml --stack-name $Stack --region $Region `
    --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND --no-fail-on-empty-changeset --parameter-overrides @params

$Url = (aws cloudformation describe-stacks --stack-name $Stack --region $Region --query "Stacks[0].Outputs[?OutputKey=='AppUrl'].OutputValue" --output text)
if (-not $SkipSmoke) {
  Write-Host "[5/5] Live smoke test (one full research run, ~2-3 min)"
  & .venv\Scripts\python.exe scripts\smoke_deploy.py $Url
}
Write-Host "`nApp: $Url"
