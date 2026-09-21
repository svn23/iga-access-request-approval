import os
from app.config import settings
from app.services.llm.base import BaseLLMProvider
from app.services.llm.gemini import GeminiProvider
from app.services.llm.openai import OpenAIProvider
from app.services.llm.anthropic import AnthropicProvider
from app.services.llm.groq import GroqProvider

def get_llm_provider() -> BaseLLMProvider:
    """
    Resolves and returns the configured LLM provider instance.
    
    Returns:
        BaseLLMProvider: The initialized LLM provider client.
    """
    provider_name = settings.llm_provider.lower().strip()
    model = settings.llm_model
    
    if provider_name == "gemini":
        api_key = settings.gemini_api_key or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY is not configured in settings or environment.")
        return GeminiProvider(api_key=api_key, model=model)
        
    elif provider_name in ("openai", "gpt"):
        api_key = settings.openai_api_key or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY is not configured in settings or environment.")
        return OpenAIProvider(api_key=api_key, model=model, base_url=settings.openai_api_base)
        
    elif provider_name == "groq":
        api_key = settings.groq_api_key or os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise ValueError("GROQ_API_KEY is not configured in settings or environment.")
        return GroqProvider(api_key=api_key, model=model)
        
    elif provider_name in ("anthropic", "claude"):
        api_key = settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is not configured in settings or environment.")
        return AnthropicProvider(api_key=api_key, model=model)
        
    else:
        raise ValueError(
            f"Unsupported LLM provider '{settings.llm_provider}'. "
            "Supported values: 'gemini', 'openai', 'groq', 'anthropic'."
        )
