from typing import List, Optional, Dict
from pydantic import BaseModel, Field


class FeatureExtractionRequest(BaseModel):
    """Feature extraction request model."""
    code: str = Field(..., description="ArkTS source code to analyze")
    extract_level: str = Field("full", description="Extraction level: basic, full")


class CodeFeature(BaseModel):
    """Single code feature model."""
    type: str = Field(..., description="Feature type: component, function, state, lifecycle")
    name: str = Field(..., description="Feature name")
    signature: Optional[str] = Field(None, description="Function signature")
    decorators: List[str] = Field(default_factory=list, description="List of decorators")
    api_calls: List[str] = Field(default_factory=list, description="List of API calls")
    location: Optional[Dict[str, int]] = Field(None, description="Location information")


class FeatureExtractionResponse(BaseModel):
    """Feature extraction response model."""
    latency_ms: float = Field(..., description="Response latency in milliseconds")
    features: List[CodeFeature] = Field(default_factory=list, description="Extracted features")
    total_features: int = Field(..., description="Total number of features extracted")