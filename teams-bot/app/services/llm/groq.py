from typing import Optional
from app.services.llm.base import BaseLLMProvider
from groq import AsyncGroq

class GroqProvider(BaseLLMProvider):
    """
    Groq API provider using the official Groq Python Async SDK.
    """
    def __init__(self, api_key: str, model: Optional[str] = None):
        self.api_key = api_key
        # Default to llama-3.3-70b-versatile if model is not set
        self.model = model if model else "llama-3.3-70b-versatile"
        self.client = AsyncGroq(api_key=self.api_key)

    async def generate(self, prompt: str, system_instruction: Optional[str] = None) -> str:
        if not self.api_key:
            raise ValueError("GROQ_API_KEY is not configured.")

        # Build messages list
        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": prompt})

        try:
            print(f"[INFO] Calling Groq model '{self.model}' asynchronously via official SDK...")
            chat_completion = await self.client.chat.completions.create(
                messages=messages,
                model=self.model,
            )
            
            if chat_completion.choices:
                return chat_completion.choices[0].message.content or ""
            else:
                print(f"[ERROR] Groq returned no choices: {chat_completion}")
                raise ValueError("No response generation choices returned from Groq.")
                
        except Exception as e:
            print(f"[ERROR] Groq API call failed: {e}")
            raise RuntimeError(f"Groq API error: {e}")
