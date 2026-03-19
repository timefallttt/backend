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


BIGMODEL_PROVIDER_ALIASES = {"bigmodel", "zhipu", "glm"}
BIGMODEL_CHAT_COMPLETIONS_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
BIGMODEL_DEFAULT_MODEL = "glm-4.7-flash"


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
        effective_provider = (provider or LLM_REVIEW_PROVIDER).strip() or "bigmodel"
        effective_api_url = (api_url or LLM_REVIEW_API_URL).strip()
        effective_api_key = (api_key or LLM_REVIEW_API_KEY).strip()
        effective_model_name = (model_name or LLM_REVIEW_MODEL_NAME).strip()

        if effective_provider.lower() in BIGMODEL_PROVIDER_ALIASES:
            if not effective_api_url:
                effective_api_url = BIGMODEL_CHAT_COMPLETIONS_URL
            if not effective_model_name or effective_model_name == "pending":
                effective_model_name = BIGMODEL_DEFAULT_MODEL

        if not effective_api_url:
            return LlmGatewayResponse(
                provider=effective_provider,
                model_name=effective_model_name or "pending",
                response_text="",
                error_message="未配置 LLM 审阅 API 地址。",
            )

        if not effective_api_key:
            return LlmGatewayResponse(
                provider=effective_provider,
                model_name=effective_model_name or "pending",
                response_text="",
                error_message="未配置 LLM 审阅 API Key。",
            )

        messages = preview.request_body.get("messages") or self._build_messages(preview)
        payload = {
            "model": effective_model_name or BIGMODEL_DEFAULT_MODEL,
            "messages": messages,
            "stream": False,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {effective_api_key}",
            "Content-Type": "application/json; charset=utf-8",
        }
        http_request = request.Request(
            effective_api_url,
            data=body,
            headers=headers,
            method="POST",
        )

        try:
            with request.urlopen(http_request, timeout=LLM_REVIEW_TIMEOUT_SEC) as response:
                raw_text = response.read().decode("utf-8", errors="replace")
                response_text = self._extract_response_text(raw_text)
                return LlmGatewayResponse(
                    provider=effective_provider,
                    model_name=effective_model_name or BIGMODEL_DEFAULT_MODEL,
                    response_text=response_text,
                )
        except error.HTTPError as exc:
            raw_text = exc.read().decode("utf-8", errors="replace")
            detail = self._extract_error_detail(raw_text)
            return LlmGatewayResponse(
                provider=effective_provider,
                model_name=effective_model_name or BIGMODEL_DEFAULT_MODEL,
                response_text=raw_text,
                error_message=f"LLM 审阅请求失败，HTTP {exc.code}。{detail}".strip(),
            )
        except Exception as exc:
            return LlmGatewayResponse(
                provider=effective_provider,
                model_name=effective_model_name or BIGMODEL_DEFAULT_MODEL,
                response_text="",
                error_message=f"LLM 审阅请求失败：{exc}",
            )

    def _build_messages(self, preview: LlmRequestPreview) -> list[dict]:
        messages = []
        if preview.system_message.strip():
            messages.append({"role": "system", "content": preview.system_message})
        if preview.user_message.strip():
            messages.append({"role": "user", "content": preview.user_message})
        return messages

    def _extract_response_text(self, raw_text: str) -> str:
        try:
            payload = json.loads(raw_text)
        except Exception:
            return raw_text

        if not isinstance(payload, dict):
            return raw_text

        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return raw_text

        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            return raw_text

        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, (dict, list)):
            return json.dumps(content, ensure_ascii=False)
        return str(content)

    def _extract_error_detail(self, raw_text: str) -> str:
        try:
            payload = json.loads(raw_text)
        except Exception:
            return raw_text[:300]

        if not isinstance(payload, dict):
            return raw_text[:300]

        error_payload = payload.get("error")
        if isinstance(error_payload, dict):
            message = error_payload.get("message")
            code = error_payload.get("code")
            if message and code:
                return f"{message} (code: {code})"
            if message:
                return str(message)

        message = payload.get("message")
        if message:
            return str(message)
        return raw_text[:300]
