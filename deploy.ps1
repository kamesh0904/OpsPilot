# deploy.ps1
# ──────────
# Automates the deployment of OpsPilot to Google Cloud Run.
# Run this script using PowerShell.

$ErrorActionPreference = "Stop"

# Colors for output
function Write-Header($msg) {
    Write-Host "`n=== $msg ===" -ForegroundColor Cyan
}
function Write-Success($msg) {
    Write-Host "[OK] $msg" -ForegroundColor Green
}
function Write-Info($msg) {
    Write-Host "[INFO] $msg" -ForegroundColor Blue
}
function Write-WarningMsg($msg) {
    Write-Host "[WARN] $msg" -ForegroundColor Yellow
}
function Write-ErrorMsg($msg) {
    Write-Host "[ERROR] $msg" -ForegroundColor Red
}

# --- Step 1: Check/Install gcloud CLI ---
Write-Header "Checking Google Cloud SDK (gcloud CLI)"

$gcloudPath = Get-Command gcloud.cmd -ErrorAction SilentlyContinue
if (-not $gcloudPath) {
    # Check default installation paths to auto-refresh PATH
    $defaultPaths = @(
        "$env:LocalAppData\Google\Cloud SDK\google-cloud-sdk\bin",
        "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin",
        "C:\Program Files\Google\Cloud SDK\google-cloud-sdk\bin"
    )
    foreach ($path in $defaultPaths) {
        if (Test-Path "$path\gcloud.cmd") {
            $env:Path += ";$path"
            $gcloudPath = Get-Command gcloud.cmd -ErrorAction SilentlyContinue
            break
        }
    }
}

if (-not $gcloudPath) {
    Write-WarningMsg "gcloud CLI was not found in your system PATH."
    Write-Info "Downloading the official Google Cloud SDK installer..."
    
    $tempInstaller = "$env:Temp\GoogleCloudSDKInstaller.exe"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri "https://dl.google.com/dl/cloudsdk/channels/rapid/GoogleCloudSDKInstaller.exe" -OutFile $tempInstaller -UseBasicParsing
        Write-Success "Installer downloaded successfully to $tempInstaller."
        Write-Info "Launching the installer. Please complete the installer GUI wizard..."
        Start-Process $tempInstaller -Wait
        
        Write-Header "Action Required"
        Write-WarningMsg "Google Cloud SDK installation has completed."
        Write-WarningMsg "Please restart your terminal/IDE to refresh your environment PATH, and run this script again."
        exit 0
    }
    catch {
        Write-ErrorMsg "Failed to download/run the installer: $_"
        exit 1
    }
} else {
    Write-Success "gcloud CLI is installed at: $($gcloudPath.Source)"
}

# --- Step 2: Authenticate with GCP ---
Write-Header "Authenticating with Google Cloud"
Write-Info "Checking login status..."
$account = & gcloud.cmd config get-value account 2>$null
if (-not $account) {
    Write-Info "No active account found. Redirecting to browser login..."
    & gcloud.cmd auth login
} else {
    Write-Success "Already authenticated as: $account"
}

# --- Step 3: Configure Project ---
Write-Header "Project Configuration"
$configuredProject = & gcloud.cmd config get-value project 2>$null
if ($configuredProject) {
    Write-Info "Using currently configured project: $configuredProject"
    $projectId = $configuredProject
}

if (-not $projectId) {
    $projectId = Read-Host "Please enter your GCP Project ID"
    if (-not $projectId) {
        Write-ErrorMsg "GCP Project ID is required."
        exit 1
    }
    Write-Info "Setting active project to '$projectId'..."
    & gcloud.cmd config set project $projectId
}

# --- Step 4: Enable Required APIs ---
Write-Header "Enabling Required Google Cloud APIs"
Write-Info "Enabling run.googleapis.com, cloudbuild.googleapis.com, and artifactregistry.googleapis.com..."
& gcloud.cmd services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com
Write-Success "All APIs enabled successfully."

# --- Step 5: Setup Artifact Registry Repository ---
Write-Header "Artifact Registry Configuration"
$region = "us-central1"
$repoName = "opspilot-repo"

Write-Info "Checking if Artifact Registry repository '$repoName' exists in '$region'..."
$repoExists = $false
try {
    $describeOutput = & gcloud.cmd artifacts repositories describe $repoName --location=$region 2>$null
    if ($describeOutput) {
        $repoExists = $true
    }
} catch {
    # Repository does not exist or permission issue; treat as not existing
    $repoExists = $false
}
if (-not $repoExists) {
    Write-Info "Creating repository '$repoName'..."
    & gcloud.cmd artifacts repositories create $repoName `
        --repository-format=docker `
        --location=$region `
        --description="Docker repository for OpsPilot"
    Write-Success "Artifact Registry repository created."
} else {
    Write-Success "Artifact Registry repository already exists."
}

# --- Step 6: Build Container Image ---
Write-Header "Building Container Image using Cloud Build"
$imageTag = "$region-docker.pkg.dev/$projectId/$repoName/opspilot:latest"
Write-Info "Submitting build directory to Cloud Build with image tag: $imageTag"
& gcloud.cmd builds submit --tag $imageTag
Write-Success "Container image built and pushed to Artifact Registry."

# --- Step 7: Clean and Prepare Env File ---
Write-Header "Preparing Environment Variables"
if (-not (Test-Path ".env")) {
    Write-ErrorMsg ".env file not found in current directory!"
    exit 1
}

Write-Info "Creating cleaned temporary env file (gcp.env) without inline comments..."
$cleanLines = @()
Get-Content ".env" | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith("#")) {
        if ($line -match "^([^#=]+)=(.*)$") {
            $key = $Matches[1].Trim()
            $val = $Matches[2].Trim()
            if ($val -match "^([^#]*)(#.*)$") {
                $val = $Matches[1].Trim()
            }
            # Remove bounding quotes if any
            if ($val -match '^"(.*)"$') {
                $val = $Matches[1]
            } elseif ($val -match "^'(.*)'$") {
                $val = $Matches[1]
            }
            $cleanLines += "$key=$val"
        }
    }
}
$cleanLines | Out-File -FilePath "gcp.env" -Encoding ascii
Write-Success "Created gcp.env."

# --- Step 8: Deploy to Cloud Run ---
Write-Header "Deploying OpsPilot to Google Cloud Run"
try {
    & gcloud.cmd run deploy opspilot `
        --image $imageTag `
        --platform managed `
        --region $region `
        --min-instances 1 `
        --memory 512Mi `
        --env-vars-file "gcp.env" `
        --allow-unauthenticated
    
    Write-Success "OpsPilot deployed successfully!"
}
catch {
    Write-ErrorMsg "Deployment failed: $_"
}
finally {
    if (Test-Path "gcp.env") {
        Write-Info "Cleaning up temporary env file..."
        Remove-Item "gcp.env" -Force
    }
}
