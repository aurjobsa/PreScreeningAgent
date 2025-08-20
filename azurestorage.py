import os
import uuid
from datetime import datetime
from typing import List

from azure.storage.blob import BlobServiceClient, ContentSettings
from fastapi import UploadFile, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Response models
class ResumeUploadResponse(BaseModel):
    resumes: List[str]

class JobDescriptionUploadResponse(BaseModel):
    job_descriptions: str

class UploadResponse(BaseModel):  # Legacy compatibility
    resumes: List[str]
    job_descriptions: List[str]

print( os.getenv("AZURE_STORAGE_CONNECTION_STRING"))
# Azure Blob Storage Configuration
class AzureBlobConfig:
    def __init__(self):
        self.connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
        
        self.container_name = os.getenv("AZURE_CONTAINER_NAME")
        
        

        if not self.connection_string or not self.container_name:
            raise ValueError("Missing Azure Storage configuration in environment variables")

        self.blob_service_client = BlobServiceClient.from_connection_string(self.connection_string)
        self.container_client = self.blob_service_client.get_container_client(self.container_name)

# Initialize Azure config
azure_config = AzureBlobConfig()

def validate_pdf_file(file: UploadFile) -> bool:
    """Validate if the uploaded file is a PDF"""
    return file.content_type == "application/pdf"

def generate_unique_filename(original_filename: str) -> str:
    """Generate a unique filename using timestamp and UUID"""
    timestamp = int(datetime.now().timestamp() * 1000)
    unique_id = str(uuid.uuid4())[:8]
    name, ext = os.path.splitext(original_filename)
    return f"{timestamp}{unique_id}{name}{ext}"

async def upload_file_to_azure(file: UploadFile, folder: str) -> str:
    """Upload file to Azure Blob Storage and return its URL"""
    try:
        # Generate unique blob name
        unique_filename = generate_unique_filename(file.filename)
        blob_path = f"{folder}/{unique_filename}"

        # Read file content
        file_content = await file.read()

        # Upload to Azure
        blob_client = azure_config.container_client.get_blob_client(blob_path)
        blob_client.upload_blob(
            file_content,
            overwrite=True,
            content_settings=ContentSettings(content_type=file.content_type)
        )

        # Generate blob URL
        blob_url = blob_client.url
        return blob_url

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Azure upload failed: {str(e)}")

def delete_file_from_azure(blob_path: str):
    """Delete a blob from Azure Storage"""
    try:
        blob_client = azure_config.container_client.get_blob_client(blob_path)
        blob_client.delete_blob()
        print(f"Deleted {blob_path} from Azure container {azure_config.container_name}")
    except Exception as e:
        print(f"Error deleting blob {blob_path}: {str(e)}")
        
