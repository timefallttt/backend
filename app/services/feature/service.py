import time
from typing import List

from .data_models import (
    FeatureExtractionRequest,
    FeatureExtractionResponse,
    CodeFeature
)


class FeatureService:
    """Feature extraction service, responsible for extracting features from ArkTS code."""
    
    def extract_features(self, request: FeatureExtractionRequest) -> FeatureExtractionResponse:
        """
        Extract features from ArkTS code
        
        Args:
            request: Feature extraction request parameters
            
        Returns:
            FeatureExtractionResponse: Extracted features response
        """
        # Record start time
        start_time = time.time()
        
        # Parse code and extract features
        features = self._extract_features_from_code(request.code, request.extract_level)
        
        # Calculate response time
        latency_ms = (time.time() - start_time) * 1000
        
        # Build response
        return FeatureExtractionResponse(
            latency_ms=latency_ms,
            features=features,
            total_features=len(features)
        )
    
    def _extract_features_from_code(self, code: str, extract_level: str) -> List[CodeFeature]:
        """
        Extract features from code
        
        Args:
            code: ArkTS source code
            extract_level: Extraction level
            
        Returns:
            List[CodeFeature]: Extracted features
        """
        # In a real implementation, this would use ArkAnalyzer
        # For now, we'll use a mock implementation
        features = []
        
        # Mock component feature extraction
        if '@Component' in code:
            features.append(CodeFeature(
                type="component",
                name="MyComponent",
                decorators=["@Component"],
                location={"start_line": 1, "end_line": 20}
            ))
        
        # Mock function feature extraction
        if 'function' in code or 'fun' in code:
            features.append(CodeFeature(
                type="function",
                name="onClick",
                signature="onClick(): void",
                api_calls=["console.log"],
                location={"start_line": 5, "end_line": 8}
            ))
        
        # Mock state feature extraction
        if '@State' in code:
            features.append(CodeFeature(
                type="state",
                name="count",
                decorators=["@State"],
                location={"start_line": 2, "end_line": 2}
            ))
        
        # Mock lifecycle feature extraction
        if 'aboutToAppear' in code or 'aboutToDisappear' in code:
            features.append(CodeFeature(
                type="lifecycle",
                name="aboutToAppear",
                signature="aboutToAppear(): void",
                location={"start_line": 10, "end_line": 12}
            ))
        
        return features