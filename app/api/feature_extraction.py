from fastapi import APIRouter, HTTPException
from app.services.feature.service import FeatureService
from app.services.feature.data_models import FeatureExtractionRequest, FeatureExtractionResponse

router = APIRouter()
feature_service = FeatureService()

@router.post("/extract", response_model=FeatureExtractionResponse)
async def extract_features(request: FeatureExtractionRequest):
    """
    Extract features from ArkTS code
    
    Args:
        request: Feature extraction request parameters
        
    Returns:
        FeatureExtractionResponse: Extracted features response
    """
    try:
        result = feature_service.extract_features(request)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))