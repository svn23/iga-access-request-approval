import httpx
from typing import Optional
from app.services.llm.base import BaseLLMProvider

class GeminiProvider(BaseLLMProvider):
    """
    Google Gemini API provider using direct asynchronous HTTP requests.
    """
    def __init__(self, api_key: str, model: Optional[str] = None):
        self.api_key = api_key
        # Default to stable gemini-1.5-flash if model is not set
        self.model = model if model else "gemini-1.5-flash"
        self.base_url = "https://generativelanguage.googleapis.com/v1beta"

    async def generate(self, prompt: str, system_instruction: Optional[str] = None) -> str:
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY is not configured.")

        url = f"{self.base_url}/models/{self.model}:generateContent?key={self.api_key}"
        headers = {
            "Content-Type": "application/json"
        }

        # Build payload
        contents_payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt}
                    ]
                }
            ]
        }

        if system_instruction:
            contents_payload["systemInstruction"] = {
                "parts": [
                    {"text": system_instruction}
                ]
            }

        try:
            print(f"[INFO] Calling Gemini model '{self.model}' asynchronously...")
            async with httpx.AsyncClient() as client:
                res = await client.post(url, json=contents_payload, headers=headers, timeout=30.0)
            
            if res.status_code == 200:
                res_data = res.json()
                try:
                    candidates = res_data.get("candidates", [])
                    if candidates:
                        text_response = candidates[0].get("content", {}).get("parts", [])[0].get("text", "")
                        return text_response
                    else:
                        print(f"[ERROR] Gemini returned no candidates: {res_data}")
                        raise ValueError("No response generation candidates returned from Gemini.")
                except (KeyError, IndexError, TypeError) as e:
                    print(f"[ERROR] Failed to parse Gemini response payload: {res.text}")
                    raise ValueError(f"Failed to parse response: {e}")
            else:
                print(f"[ERROR] Gemini API request failed: {res.status_code} - {res.text}")
                raise ValueError(f"Gemini API error: {res.status_code} - {res.text}")

        except httpx.HTTPError as e:
            print(f"[ERROR] Network exception when calling Gemini API: {e}")
            raise RuntimeError(f"Network error calling Gemini: {e}")
