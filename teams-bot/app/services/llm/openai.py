import httpx
from typing import Optional
from app.services.llm.base import BaseLLMProvider

class OpenAIProvider(BaseLLMProvider):
    """
    OpenAI API provider using direct asynchronous HTTP requests.
    """
    def __init__(self, api_key: str, model: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = api_key
        # Default to gpt-4o-mini if model is not set
        self.model = model if model else "gpt-4o-mini"
        self.url = f"{base_url.rstrip('/')}/chat/completions" if base_url else "https://api.openai.com/v1/chat/completions"

    async def generate(self, prompt: str, system_instruction: Optional[str] = None) -> str:
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is not configured.")

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }

        # Build messages payload
        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages
        }

        try:
            print(f"[INFO] Calling OpenAI model '{self.model}' asynchronously...")
            async with httpx.AsyncClient() as client:
                res = await client.post(self.url, json=payload, headers=headers, timeout=30.0)
            
            if res.status_code == 200:
                res_data = res.json()
                try:
                    choices = res_data.get("choices", [])
                    if choices:
                        text_response = choices[0].get("message", {}).get("content", "")
                        return text_response
                    else:
                        print(f"[ERROR] OpenAI returned no choices: {res_data}")
                        raise ValueError("No response generation choices returned from OpenAI.")
                except (KeyError, IndexError, TypeError) as e:
                    print(f"[ERROR] Failed to parse OpenAI response payload: {res.text}")
                    raise ValueError(f"Failed to parse response: {e}")
            else:
                print(f"[ERROR] OpenAI API request failed: {res.status_code} - {res.text}")
                raise ValueError(f"OpenAI API error: {res.status_code} - {res.text}")

        except httpx.HTTPError as e:
            print(f"[ERROR] Network exception when calling OpenAI API: {e}")
            raise RuntimeError(f"Network error calling OpenAI: {e}")
