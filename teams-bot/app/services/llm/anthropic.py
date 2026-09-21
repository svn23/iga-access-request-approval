import httpx
from typing import Optional
from app.services.llm.base import BaseLLMProvider

class AnthropicProvider(BaseLLMProvider):
    """
    Anthropic API provider using direct asynchronous HTTP requests.
    """
    def __init__(self, api_key: str, model: Optional[str] = None):
        self.api_key = api_key
        # Default to claude-3-5-sonnet-latest if model is not set
        self.model = model if model else "claude-3-5-sonnet-latest"
        self.url = "https://api.anthropic.com/v1/messages"

    async def generate(self, prompt: str, system_instruction: Optional[str] = None) -> str:
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY is not configured.")

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01"
        }

        # Build payload
        payload = {
            "model": self.model,
            "max_tokens": 1024,
            "messages": [
                {"role": "user", "content": prompt}
            ]
        }

        if system_instruction:
            payload["system"] = system_instruction

        try:
            print(f"[INFO] Calling Anthropic model '{self.model}' asynchronously...")
            async with httpx.AsyncClient() as client:
                res = await client.post(self.url, json=payload, headers=headers, timeout=30.0)
            
            if res.status_code == 200:
                res_data = res.json()
                try:
                    content_list = res_data.get("content", [])
                    if content_list and content_list[0].get("type") == "text":
                        text_response = content_list[0].get("text", "")
                        return text_response
                    else:
                        print(f"[ERROR] Anthropic returned unexpected content format: {res_data}")
                        raise ValueError("Unexpected response content format returned from Anthropic.")
                except (KeyError, IndexError, TypeError) as e:
                    print(f"[ERROR] Failed to parse Anthropic response payload: {res.text}")
                    raise ValueError(f"Failed to parse response: {e}")
            else:
                print(f"[ERROR] Anthropic API request failed: {res.status_code} - {res.text}")
                raise ValueError(f"Anthropic API error: {res.status_code} - {res.text}")

        except httpx.HTTPError as e:
            print(f"[ERROR] Network exception when calling Anthropic API: {e}")
            raise RuntimeError(f"Network error calling Anthropic: {e}")
