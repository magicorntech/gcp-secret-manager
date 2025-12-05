# Magicorn GCP Secret Sync to Kubernetes

A production-ready application that automatically synchronizes secrets from Google Cloud Platform (GCP) Secret Manager to Kubernetes secrets. This service ensures your Kubernetes applications always have access to the latest secrets from GCP.

## Features

- 🔄 Automatically fetches secrets from GCP Secret Manager
- ☸️ Writes secrets as key-value pairs to Kubernetes
- ⏰ Configurable periodic sync (default: 5 minutes)
- 🔌 REST API endpoints (`/api/sync`, `/api/health`)
- 🔐 Token-based authentication for sync endpoint
- 📊 Comprehensive logging
- 🏥 Health check endpoint
- 🌍 Unicode character normalization for Kubernetes-compliant keys

## How It Works

### Architecture Overview

```
┌─────────────────┐         ┌──────────────────┐         ┌─────────────────┐
│  GCP Secret     │         │  Magicorn Secret │         │  Kubernetes      │
│  Manager        │────────▶│  Sync Service    │────────▶│  Secret          │
│                 │  Fetch  │                  │  Update  │                  │
└─────────────────┘         └──────────────────┘         └─────────────────┘
                                      │
                                      │ Periodic Sync
                                      │ (every 5 min)
                                      ▼
                            ┌──────────────────┐
                            │  Kubernetes      │
                            │  Cluster         │
                            └──────────────────┘
```

### Workflow

1. **Initialization**: On startup, the application:
   - Initializes GCP Secret Manager client (using Workload Identity or service account credentials)
   - Initializes Kubernetes API client (using in-cluster config or kubeconfig)
   - Starts a background task for periodic synchronization

2. **Periodic Sync**: Every N seconds (configurable via `SYNC_INTERVAL_SECONDS`):
   - Fetches the latest secret version from GCP Secret Manager
   - Parses the JSON secret data
   - Normalizes secret keys (converts Unicode characters, removes invalid characters)
   - Updates or creates the Kubernetes secret in the specified namespace

3. **Manual Sync**: Via REST API:
   - Accepts POST requests to `/api/sync` endpoint
   - Requires authentication token (if `API_TOKEN` is configured)
   - Performs the same sync operation on-demand

4. **Key Normalization**: 
   - Converts Unicode characters (e.g., İ→I, ş→s) to ASCII equivalents
   - Replaces invalid characters with underscores
   - Ensures keys match Kubernetes validation: `[-._a-zA-Z0-9]+`

## Requirements

- Python 3.11+
- GCP Secret Manager access
- Kubernetes cluster access
- GCP Service Account with Workload Identity (recommended) or service account key

## Configuration

### Environment Variables

The application expects the following environment variables:

| Variable | Description | Example |
|----------|-------------|---------|
| `GCP_PROJECT_ID` | GCP project ID or number | `445422739407` |
| `GCP_SECRET_NAME` | Secret name in GCP Secret Manager | `magicorn-secret-nonprod` |
| `GCP_SECRET_VERSION` | Secret version (optional) | `latest` |
| `K8S_NAMESPACE` | Kubernetes namespace | `default` |
| `K8S_SECRET_NAME` | Kubernetes secret name | `magicorn-common-secrets` |
| `SYNC_INTERVAL_SECONDS` | Sync interval in seconds | `300` (5 minutes) |
| `API_TOKEN` | Token for `/api/sync` endpoint (optional) | `your-secret-token` |
| `GCP_CREDENTIALS_PATH` | Path to GCP credentials file (optional) | `/var/secrets/google/key.json` |

### GCP Secret Format

The secret in GCP Secret Manager must be in JSON format:

```json
{
  "NEXT_PUBLIC_SITE_URL": "https://magicorn.co",
  "NEXT_PUBLIC_APP_URL": "https://web.magicorn.co",
  "API_KEY": "secret-value-123"
}
```

## Installation

### 1. Build Docker Image

```bash
docker buildx build --platform linux/amd64 -t magicorn-gcp-secret-sync:latest .
```

### 2. Push to Container Registry

```bash
# Tag for your registry
docker tag magicorn-gcp-secret-sync:latest \
  us-central1-docker.pkg.dev/YOUR_PROJECT/magicorn-services/magicorn-gcp-secret-sync:latest

# Push
docker push us-central1-docker.pkg.dev/YOUR_PROJECT/magicorn-services/magicorn-gcp-secret-sync:latest
```

### 3. Kubernetes Deployment

1. **Update `k8s/deployment.yaml`**:
   - Set your namespace
   - Configure secret names
   - Update GCP project information
   - Set image path

2. **Configure Workload Identity** (recommended):

```bash
# Create GCP Service Account
gcloud iam service-accounts create magicorn-gcp-secret-sync \
    --project=YOUR_PROJECT_ID

# Grant Secret Manager access
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
    --member="serviceAccount:magicorn-gcp-secret-sync@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor"

# Bind Workload Identity
gcloud iam service-accounts add-iam-policy-binding \
    magicorn-gcp-secret-sync@YOUR_PROJECT_ID.iam.gserviceaccount.com \
    --role roles/iam.workloadIdentityUser \
    --member "serviceAccount:YOUR_PROJECT_ID.svc.id.goog[NAMESPACE/magicorn-gcp-secret-sync-sa]"

# Annotate Kubernetes Service Account
kubectl annotate serviceaccount magicorn-gcp-secret-sync-sa \
    iam.gke.io/gcp-service-account=magicorn-gcp-secret-sync@YOUR_PROJECT_ID.iam.gserviceaccount.com \
    -n NAMESPACE
```

3. **Create API Token Secret** (optional, for sync endpoint authentication):

```bash
kubectl create secret generic magicorn-gcp-secret-sync-api-token \
  --from-literal=token=$(openssl rand -hex 32) \
  -n NAMESPACE
```

4. **Apply RBAC**:

```bash
kubectl apply -f k8s/rbac.yaml
```

5. **Deploy**:

```bash
kubectl apply -f k8s/deployment.yaml
```

## Usage

### API Endpoints

#### Health Check

Check the health status of the service:

```bash
curl http://localhost:8000/api/health
```

Response:
```json
{
  "status": "healthy",
  "timestamp": "2024-01-01T12:00:00",
  "checks": {
    "gcp": {
      "status": "ok",
      "message": "GCP Secret Manager client initialized"
    },
    "kubernetes": {
      "status": "ok",
      "message": "Kubernetes client initialized (namespace: default)"
    }
  }
}
```

#### Manual Sync

Trigger a manual synchronization (requires token if `API_TOKEN` is set):

```bash
# With token
curl -X POST http://localhost:8000/api/sync \
  -H 'Authorization: Bearer YOUR_TOKEN'

# Without token (if API_TOKEN is not configured)
curl -X POST http://localhost:8000/api/sync
```

Response:
```json
{
  "status": "success",
  "message": "Secrets synced successfully",
  "timestamp": "2024-01-01T12:00:00"
}
```

### Monitoring Logs

```bash
# Follow logs
kubectl logs -f deployment/magicorn-gcp-secret-sync -n NAMESPACE

# Check recent logs
kubectl logs deployment/magicorn-gcp-secret-sync -n NAMESPACE --tail=100
```

### Verify Secret Sync

```bash
# Check if secret exists
kubectl get secret magicorn-common-secrets -n NAMESPACE

# View secret keys (not values)
kubectl get secret magicorn-common-secrets -n NAMESPACE -o jsonpath='{.data}' | jq 'keys'

# Decode a specific value
kubectl get secret magicorn-common-secrets -n NAMESPACE \
  -o jsonpath='{.data.KEY_NAME}' | base64 -d
```

## Development

### Local Development

1. Create `.env` file (copy from `.env.example`):
```bash
cp .env.example .env
```

2. Configure GCP credentials:
   - Use `gcloud auth application-default login` for local development
   - Or set `GOOGLE_APPLICATION_CREDENTIALS` environment variable

3. Configure Kubernetes:
   - Ensure `kubeconfig` is properly configured
   - Or use `kubectl` to set up access

4. Install dependencies and run:
```bash
pip install -r requirements.txt
python main.py
```

## Security Best Practices

- ✅ Use Workload Identity in production (no service account keys)
- ✅ Grant minimum required permissions to service accounts
- ✅ Configure Kubernetes RBAC rules carefully
- ✅ Never log secret values
- ✅ Use token authentication for sync endpoint
- ✅ Store API token in Kubernetes secrets
- ✅ Regularly rotate API tokens

## Troubleshooting

### GCP Connection Error

**Symptoms**: `TransportError` or `RefreshError` when accessing GCP Secret Manager

**Solutions**:
- Verify service account has `roles/secretmanager.secretAccessor` role
- Check Workload Identity binding is correct
- Ensure `GOOGLE_APPLICATION_CREDENTIALS` is set (if using service account key)
- Verify GCP project ID and secret name are correct

### Kubernetes Connection Error

**Symptoms**: Cannot connect to Kubernetes API

**Solutions**:
- Check kubeconfig is properly configured
- Verify Service Account RBAC permissions
- Ensure namespace exists
- Check if running in-cluster (for in-cluster config)

### Secret Key Normalization Warnings

**Symptoms**: Logs show "Secret key normalized" warnings

**Explanation**: This is normal behavior. Keys with Unicode characters or invalid characters are automatically normalized to be Kubernetes-compliant.

**Example**: `STONKİ_TEST` → `STONKI_TEST`

### 422 Unprocessable Entity Error

**Symptoms**: `(422) Reason: Unprocessable Entity` when updating secrets

**Solutions**:
- Check if secret keys contain invalid characters (should be fixed by normalization)
- Verify namespace exists
- Check RBAC permissions for secret creation/update

## Architecture Details

### Components

1. **FastAPI Application**: REST API server with async support
2. **GCP Secret Manager Client**: Fetches secrets from GCP
3. **Kubernetes API Client**: Updates secrets in Kubernetes
4. **Background Sync Task**: Periodic synchronization loop
5. **Key Normalization**: Ensures Kubernetes-compliant secret keys

### Data Flow

1. Application starts → Initializes clients
2. Background task starts → Begins periodic sync loop
3. Each sync cycle:
   - Fetch from GCP Secret Manager
   - Parse JSON
   - Normalize keys
   - Update Kubernetes secret
4. API endpoint → Manual sync trigger (same flow)

### Error Handling

- Automatic retry on sync failures (waits 60 seconds before retry)
- Detailed error logging
- Health check reflects client initialization status
- Graceful shutdown on application termination

## License

MIT
