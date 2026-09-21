from abc import ABC, abstractmethod
from typing import Optional

class BaseLLMProvider(ABC):
    """
    Abstract base class for all LLM providers (Gemini, OpenAI, Anthropic, etc.).
    """
    @abstractmethod
    async def generate(self, prompt: str, system_instruction: Optional[str] = None) -> str:
        """
        Sends a query/prompt to the LLM and returns the text response asynchronously.
        
        Args:
            prompt (str): The main prompt/input for the model.
            system_instruction (Optional[str]): System prompts or instructions to set the persona/behavior.
            
        Returns:
            str: The generated text response.
        """
        pass
