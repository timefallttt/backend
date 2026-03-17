import json
from dataclasses import dataclass
from urllib import error, request

from app.config import (
    LLM_REVIEW_API_KEY,
    LLM_REVIEW_API_URL,
    LLM_REVIEW_MODEL_NAME,
    LLM_REVIEW_PROVIDER,
    LLM_REVIEW_TIMEOUT_SEC,
)

from .schemas import LlmRequestPreview


@dataclass
class LlmGatewayResponse:
    provider: str
    model_name: str
    response_text: str
    error_message: str = ""


class LlmReviewGateway:
    def submit_review(
        self,
        preview: LlmRequestPreview,
        *,
        provider: str = "",
        api_url: str = "",
        api_key: str = "",
        model_name: str = "",
    ) -> LlmGatewayResponse:
        effective_provider = (provider or LLM_REVIEW_PROVIDER).strip() or "pending"
        effective_api_url = (api_url or LLM_REVIEW_API_URL).strip()
        effective_api_key = (api_key or LLM_REVIEW_API_KEY).strip()
        effective_model_name = (model_name or LLM_REVIEW_MODEL_NAME).strip() or "pending"

        if not effective_api_url:
            return LlmGatewayResponse(
                provider=effective_provider,
                model_name=effective_model_name,
                response_text="",
                error_message="未配置 LLM 审阅 API 地址。",
            )

        payload = {
            "model": effective_model_name,
            "request": preview.request_body,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
        }
        if effective_api_key:
            headers["Authorization"] = f"Bearer {effective_api_key}"

        http_request = request.Request(
            effective_api_url,
            data=body,
            headers=headers,
            method="POST",
        )

        try:
            with request.urlopen(http_request, timeout=LLM_REVIEW_TIMEOUT_SEC) as response:
                return LlmGatewayResponse(
                    provider=effective_provider,
                    model_name=effective_model_name,
                    response_text=response.read().decode("utf-8", errors="replace"),
                )
        except error.HTTPError as exc:
            return LlmGatewayResponse(
                provider=effective_provider,
                model_name=effective_model_name,
                response_text=exc.read().decode("utf-8", errors="replace"),
                error_message=f"LLM 审阅请求失败，HTTP {exc.code}。",
            )
        except Exception as exc:
            return LlmGatewayResponse(
                provider=effective_provider,
                model_name=effective_model_name,
                response_text="",
                error_message=f"LLM 审阅请求失败：{exc}",
            )
