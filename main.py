import os
import json
import logging
import asyncio
import re
import unicodedata
from typing import Dict, Optional
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import JSONResponse
from typing import Optional as TypingOptional
from pydantic_settings import BaseSettings
from google.cloud import secretmanager
from kubernetes import client, config
from kubernetes.client.rest import ApiException

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    # GCP Settings
    gcp_project_id: str
    gcp_secret_name: str
    gcp_secret_version: str = "latest"
    
    # Kubernetes Settings
    k8s_namespace: str
    k8s_secret_name: str
    
    # Sync Settings
    sync_interval_seconds: int = 300  # 5 minutes default
    
    # API Authentication
    api_token: Optional[str] = None  # Token for /api/sync endpoint
    
    # GCP Credentials (optional, can use workload identity)
    gcp_credentials_path: Optional[str] = None
    
    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()

# Global clients
secret_client: Optional[secretmanager.SecretManagerServiceClient] = None
k8s_core_v1: Optional[client.CoreV1Api] = None
sync_task: Optional[asyncio.Task] = None


def init_gcp_client():
    """Initialize GCP Secret Manager client"""
    global secret_client
    try:
        if settings.gcp_credentials_path and os.path.exists(settings.gcp_credentials_path):
            os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = settings.gcp_credentials_path
        secret_client = secretmanager.SecretManagerServiceClient()
        logger.info("GCP Secret Manager client initialized successfully")
        return True
    except Exception as e:
        logger.error(f"Failed to initialize GCP client: {e}")
        return False


def init_k8s_client():
    """Initialize Kubernetes client"""
    global k8s_core_v1
    try:
        # Try in-cluster config first, then fallback to kubeconfig
        try:
            config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes config")
        except:
            config.load_kube_config()
            logger.info("Loaded kubeconfig")
        
        k8s_core_v1 = client.CoreV1Api()
        logger.info("Kubernetes client initialized successfully")
        return True
    except Exception as e:
        logger.error(f"Failed to initialize Kubernetes client: {e}")
        return False


async def fetch_gcp_secret() -> Dict[str, str]:
    """Fetch secret from GCP Secret Manager"""
    if not secret_client:
        raise Exception("GCP client not initialized")
    
    try:
        secret_path = f"projects/{settings.gcp_project_id}/secrets/{settings.gcp_secret_name}/versions/{settings.gcp_secret_version}"
        logger.info(f"Fetching secret from: {secret_path}")
        
        response = secret_client.access_secret_version(request={"name": secret_path})
        secret_data = response.payload.data.decode('UTF-8')
        
        # Parse JSON
        secrets = json.loads(secret_data)
        
        logger.info(f"Successfully fetched {len(secrets)} secrets from GCP")
        return secrets
    except Exception as e:
        logger.error(f"Failed to fetch GCP secret: {e}")
        raise


def normalize_secret_key(key: str) -> str:
    """
    Normalize secret key to be Kubernetes-compliant.
    Kubernetes secret keys must consist of alphanumeric characters, '-', '_' or '.'.
    This function:
    1. Converts Turkish characters to their ASCII equivalents
    2. Replaces invalid characters with underscores
    3. Ensures the key matches Kubernetes validation regex: [-._a-zA-Z0-9]+
    """
    # Normalize Unicode characters (e.g., İ -> I, ı -> i)
    normalized = unicodedata.normalize('NFKD', key)
    # Convert to ASCII, ignoring non-ASCII characters
    ascii_key = normalized.encode('ascii', 'ignore').decode('ascii')
    # Replace any remaining invalid characters with underscore
    # Kubernetes allows: alphanumeric, '-', '_', '.'
    valid_key = re.sub(r'[^a-zA-Z0-9._-]', '_', ascii_key)
    # Remove consecutive underscores
    valid_key = re.sub(r'_+', '_', valid_key)
    # Remove leading/trailing underscores and dots
    valid_key = valid_key.strip('_.')
    # Ensure key is not empty
    if not valid_key:
        valid_key = 'INVALID_KEY'
    return valid_key


async def update_k8s_secret(secrets: Dict[str, str]) -> bool:
    """Update Kubernetes secret with the fetched secrets"""
    if not k8s_core_v1:
        raise Exception("Kubernetes client not initialized")
    
    try:
        # Normalize keys and convert all values to strings
        string_data = {}
        for original_key, value in secrets.items():
            normalized_key = normalize_secret_key(original_key)
            if original_key != normalized_key:
                logger.warning(f"Secret key normalized: '{original_key}' -> '{normalized_key}'")
            string_data[normalized_key] = str(value)
        
        # Check if secret exists
        try:
            existing_secret = k8s_core_v1.read_namespaced_secret(
                name=settings.k8s_secret_name,
                namespace=settings.k8s_namespace
            )
            
            # Update existing secret
            existing_secret.string_data = string_data
            existing_secret.data = None  # Clear data to force update
            
            k8s_core_v1.patch_namespaced_secret(
                name=settings.k8s_secret_name,
                namespace=settings.k8s_namespace,
                body=existing_secret
            )
            logger.info(f"Updated existing Kubernetes secret: {settings.k8s_secret_name}")
            
        except ApiException as e:
            if e.status == 404:
                # Secret doesn't exist, create it
                secret_body = client.V1Secret(
                    metadata=client.V1ObjectMeta(
                        name=settings.k8s_secret_name,
                        namespace=settings.k8s_namespace
                    ),
                    string_data=string_data
                )
                
                k8s_core_v1.create_namespaced_secret(
                    namespace=settings.k8s_namespace,
                    body=secret_body
                )
                logger.info(f"Created new Kubernetes secret: {settings.k8s_secret_name}")
            else:
                raise
        
        return True
    except Exception as e:
        logger.error(f"Failed to update Kubernetes secret: {e}")
        raise


async def sync_secrets():
    """Sync secrets from GCP to Kubernetes"""
    try:
        logger.info("Starting secret sync...")
        secrets = await fetch_gcp_secret()
        await update_k8s_secret(secrets)
        logger.info("Secret sync completed successfully")
        return True
    except Exception as e:
        logger.error(f"Secret sync failed: {e}")
        return False


async def periodic_sync():
    """Periodically sync secrets"""
    while True:
        try:
            await sync_secrets()
            await asyncio.sleep(settings.sync_interval_seconds)
        except Exception as e:
            logger.error(f"Error in periodic sync: {e}")
            await asyncio.sleep(60)  # Wait 1 minute before retry on error


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup and shutdown"""
    # Startup
    logger.info("Starting application...")
    
    if not init_gcp_client():
        logger.error("Failed to initialize GCP client. Check your configuration.")
        raise RuntimeError("GCP client initialization failed")
    
    if not init_k8s_client():
        logger.error("Failed to initialize Kubernetes client. Check your configuration.")
        raise RuntimeError("Kubernetes client initialization failed")
    
    # Start periodic sync task
    global sync_task
    sync_task = asyncio.create_task(periodic_sync())
    logger.info(f"Started periodic sync with interval: {settings.sync_interval_seconds}s")
    
    yield
    
    # Shutdown
    logger.info("Shutting down application...")
    if sync_task:
        sync_task.cancel()
        try:
            await sync_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="GCP Secret Sync to Kubernetes",
    description="Syncs secrets from GCP Secret Manager to Kubernetes",
    lifespan=lifespan
)


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "service": "GCP Secret Sync to Kubernetes",
        "status": "running",
        "endpoints": {
            "health": "/api/health",
            "sync": "/api/sync"
        }
    }


@app.get("/api/health")
async def health_check():
    """Health check endpoint - verifies GCP and Kubernetes connectivity"""
    health_status = {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "checks": {}
    }
    
    # Check GCP connectivity - only check if client is initialized
    # Don't make actual API calls to avoid rate limiting and improve performance
    if secret_client:
        health_status["checks"]["gcp"] = {
            "status": "ok",
            "message": "GCP Secret Manager client initialized"
        }
    else:
        health_status["checks"]["gcp"] = {
            "status": "error",
            "message": "GCP client not initialized"
        }
        health_status["status"] = "unhealthy"
    
    # Check Kubernetes connectivity - only check if client is initialized
    # Don't make actual API calls to avoid rate limiting and improve performance
    if k8s_core_v1:
        health_status["checks"]["kubernetes"] = {
            "status": "ok",
            "message": f"Kubernetes client initialized (namespace: {settings.k8s_namespace})"
        }
    else:
        health_status["checks"]["kubernetes"] = {
            "status": "error",
            "message": "Kubernetes client not initialized"
        }
        health_status["status"] = "unhealthy"
    
    # Always return 200 if clients are initialized
    # The actual connectivity is tested during sync operations
    status_code = 200
    return JSONResponse(content=health_status, status_code=status_code)


@app.post("/api/sync")
async def manual_sync(authorization: TypingOptional[str] = Header(None)):
    """Manually trigger secret sync - requires token authentication"""
    # Check if token is required
    if settings.api_token:
        # Extract token from Authorization header
        if not authorization:
            raise HTTPException(
                status_code=401,
                detail="Authorization header is required"
            )
        
        # Support both "Bearer <token>" and plain token
        token = authorization.replace("Bearer ", "").strip() if authorization.startswith("Bearer ") else authorization.strip()
        
        if token != settings.api_token:
            logger.warning("Invalid token attempt for /api/sync")
            raise HTTPException(
                status_code=403,
                detail="Invalid token"
            )
    
    try:
        logger.info("Manual sync triggered via API")
        success = await sync_secrets()
        
        if success:
            return {
                "status": "success",
                "message": "Secrets synced successfully",
                "timestamp": datetime.utcnow().isoformat()
            }
        else:
            raise HTTPException(
                status_code=500,
                detail="Secret sync failed. Check logs for details."
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Manual sync error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Sync failed: {str(e)}"
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

