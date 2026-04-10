#!/bin/bash
set -e

# Configuration - Edit these values before running
# Enter your Google Cloud project ID (find it at: https://console.cloud.google.com)
PROJECT_ID="project-ID"

# Enter a name for your Cloud Run service
SERVICE_NAME="gti-mcp-server"

# Enter your preferred Google Cloud region (e.g., us-central1, us-east1, europe-west1)
REGION="europe-west2"

# NOTE: Replace these with your actual secrets or set them in your environment before running
# You can also use Google Secret Manager references in Cloud Run for better security.
# Generate a random token if not provided
#AUTH_TOKEN=${MCP_AUTH_TOKEN:-$(openssl rand -hex 32)}
AUTH_TOKEN=${MCP_AUTH_TOKEN:-$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')}
# VT_KEY is now passed via tool arguments
# VT_KEY=${VT_APIKEY:-"change-me-to-your-actual-vt-api-key"}

echo "=================================================="
echo "Deploying $SERVICE_NAME to project $PROJECT_ID"
echo "Region: $REGION"
echo "=================================================="

# --- PREREQUISITE CHECKS ---

# 1. Check if authenticated
echo "Checking gcloud authentication..."
if ! gcloud auth print-access-token > /dev/null 2>&1; then
  echo "ERROR: You are not authenticated with gcloud."
  echo "Please run 'gcloud auth login' and 'gcloud auth application-default login' then try again."
  exit 1
fi

# 2. Check if project exists and is accessible
echo "Verifying project $PROJECT_ID..."
if ! gcloud projects describe "$PROJECT_ID" > /dev/null 2>&1; then
  echo "ERROR: Project $PROJECT_ID not found or not accessible."
  echo "Please check the PROJECT_ID at the top of this script."
  exit 1
fi

# 3. Check if billing is enabled
echo "Checking if billing is enabled..."
BILLING_ENABLED=$(gcloud beta billing projects describe "$PROJECT_ID" --format='value(billingEnabled)' 2>/dev/null || echo "unknown")
if [ "$BILLING_ENABLED" = "false" ]; then
  echo "ERROR: Billing is not enabled for project $PROJECT_ID."
  echo "Cloud Run and Cloud Build require an active billing account."
  echo "Enable billing at: https://console.cloud.google.com/billing"
  exit 1
elif [ "$BILLING_ENABLED" = "unknown" ]; then
  echo "Warning: Could not verify billing status. Continuing anyway..."
fi

# --- END PREREQUISITE CHECKS ---

# Ensure the correct project is active
echo "Configuring gcloud for project $PROJECT_ID..."
gcloud config set project "$PROJECT_ID" --quiet
# Suppress the quota project warning for this session
export CLOUDSDK_CORE_QUOTA_PROJECT="$PROJECT_ID"

# Enable required APIs
echo "Enabling required APIs (Cloud Run, Cloud Build, Artifact Registry)..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  storage-api.googleapis.com \
  --project "$PROJECT_ID"

# Prepare IAM permissions for the default Compute Engine service account
# This is often needed when using 'gcloud run deploy --source'
echo "Configuring IAM permissions for the default service account..."
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
COMPUTE_SVC_ACCOUNT="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

# Grant Storage Object Viewer to the compute service account so Cloud Build can access the source
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${COMPUTE_SVC_ACCOUNT}" \
  --role="roles/storage.objectViewer" \
  --quiet > /dev/null

# Grant Artifact Registry Writer to the compute service account so it can push images
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${COMPUTE_SVC_ACCOUNT}" \
  --role="roles/artifactregistry.writer" \
  --quiet > /dev/null

# Deploy to Cloud Run using source deploy
# This automatically builds the container using Google Cloud Buildpacks
# and handles the Artifact Registry creation/management.
echo "Deploying service to Cloud Run (source deploy)..."
# We capture the output to check for IAM failures
DEPLOY_OUTPUT=$(gcloud run deploy "$SERVICE_NAME" \
  --source . \
  --platform managed \
  --region "$REGION" \
  --project "$PROJECT_ID" \
  --allow-unauthenticated \
  --set-env-vars MCP_AUTH_TOKEN="$AUTH_TOKEN" \
  --set-env-vars STATELESS="1" \
  --quiet 2>&1) || { echo "$DEPLOY_OUTPUT"; exit 1; }

echo "$DEPLOY_OUTPUT"

# Check if the unauthenticated binding failed (common in restricted orgs)
if echo "$DEPLOY_OUTPUT" | grep -q "Setting IAM policy failed"; then
  echo ""
  echo "⚠️  WARNING: Could not make the service publicly accessible."
  echo "This is likely due to an Organization Policy (Domain Restricted Sharing)."
  echo "The service was deployed but is currently PRIVATE."
  echo "Most MCP clients will NOT be able to connect unless you fix this."
  echo "Fix: Check IAM Organization Policy 'iam.allowedPolicyMemberDomains'."
  echo ""
fi

echo "=================================================="
echo "Deployment Complete!"
echo "Service URL: $(gcloud run services describe $SERVICE_NAME --platform managed --region $REGION --project $PROJECT_ID --format 'value(status.url)')"
echo "SSE Endpoint: $(gcloud run services describe $SERVICE_NAME --platform managed --region $REGION --project $PROJECT_ID --format 'value(status.url)')/sse"
echo "Auth Token: $AUTH_TOKEN"
echo "=================================================="
