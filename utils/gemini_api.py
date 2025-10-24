import os
from google import genai
from google.genai import types
from dotenv import load_dotenv
from .rate_limiter import RateLimitTracker
from logs.logger import logger

class GeminiClient:
    """A robust client for interacting with the Gemini API."""
    
    def __init__(self, rate_limiter: RateLimitTracker):
        """
        Initializes the Gemini client.
        """
        try:
            load_dotenv()
            self.api_key = os.getenv("GEMINI_API_KEY")
            if not self.api_key:
                raise ValueError("GEMINI_API_KEY not found.")
            
            # Initialize the new client
            self.client = genai.Client(api_key=self.api_key)
            logger.info("Gemini API configured successfully.")
        except Exception as e:
            logger.fatal(f"Failed to configure Gemini API: {e}")
            self.client = None
            return

        self.rate_limiter = rate_limiter 

        self.model_map = {
            'tier1': 'gemini-2.5-pro',
            'tier2': 'gemini-2.5-flash',
            'tier3': 'gemini-2.5-flash-lite'
        }
        
        # Create grounding tool for search
        self.grounding_tool = types.Tool(
            google_search=types.GoogleSearch()
        )
        
        self.search_config = types.GenerateContentConfig(
            tools=[self.grounding_tool]
        )

    def ask_gemini(self, prompt: str, tier: str, enable_search: bool = False) -> str | None:
        """
        Sends a prompt to the specified Gemini model tier.
        Can optionally enable grounded web search for the query.
        """
        if not self.client:
            logger.error("Gemini client not initialized.")
            return None
            
        if tier not in self.model_map:
            logger.error(f"Invalid tier '{tier}'.")
            return None

        if not self.rate_limiter.check_and_increment(tier):
            logger.warning(f"API call to {tier} blocked by internal rate limiter.")
            return None

        try:
            model_name = self.model_map[tier]
            
            if enable_search:
                logger.info("Executing prompt with Google Search enabled.")
                response = self.client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=self.search_config
                )
            else:
                response = self.client.models.generate_content(
                    model=model_name,
                    contents=prompt
                )
            
            if not response.text:
                logger.warning(f"Gemini API returned no content for tier {tier}.")
                return None
            
            return response.text
        except Exception as e:
            logger.error(f"Unexpected error during Gemini API call for tier {tier}: {e}")
            return None