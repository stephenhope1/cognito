import os
import re
import time
from google import genai
from google.genai import types
from google.api_core import exceptions
from dotenv import load_dotenv
from .rate_limiter import RateLimitTracker
from .logger import logger

class GeminiClient:
    """A robust client for a modern Google GenAI SDK, with structured output support."""
    
    def __init__(self, rate_limiter: RateLimitTracker):
        """Initializes the Gemini client."""
        try:
            load_dotenv()
            self.client = genai.Client()
            logger.info("Gemini API configured successfully using genai.Client.")
        except Exception as e:
            logger.fatal(f"Failed to configure Gemini API: {e}")
            self.client = None
            return

        self.rate_limiter = rate_limiter 

        self.model_map = {
            'tier1': 'gemini-2.5-pro',
            'tier2': 'gemini-2.5-flash',
        }
        
        self.grounding_tool = types.Tool(
            google_search=types.GoogleSearch()
        )
        self.search_config = types.GenerateContentConfig(
            tools=[self.grounding_tool]
        )

    def ask_gemini(self, prompt: str, tier: str, enable_search: bool = False, response_schema=None) -> types.GenerateContentResponse | None | str:
        """
        Sends a prompt to the specified Gemini model tier.
        - Supports search, structured output, and intelligent rate limit handling.
        - Returns the full response object, a RATE_LIMIT_HIT string, or None.
        """
        if not self.client:
            logger.error("Gemini client not initialized.")
            return None
            
        if tier not in self.model_map:
            logger.error(f"Invalid tier '{tier}'.")
            return None

        if not self.rate_limiter.check_and_increment(tier):
            logger.warning(f"API call to {tier} blocked by internal rate limiter.")
            return "RATE_LIMIT_HIT"

        try:
            model_name = self.model_map[tier]
            # Dynamically build the configuration for this specific call
            # This is the modern, correct way to pass all optional parameters.
            generation_config = None
            if response_schema:
                # For structured output, build a dict as per the documentation.
                logger.info(f"Executing prompt with structured output schema.")
                generation_config = {
                    "response_mime_type": "application/json",
                    "response_schema": response_schema
                }
            elif enable_search:
                # For search, use the pre-built GenerateContentConfig object.
                logger.info("Executing prompt with Google Search enabled.")
                generation_config = self.search_config
            
            response = self.client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=generation_config # Pass the correct config object
            )
            
            return response
            
        except exceptions.ResourceExhausted as e:
            logger.warning(f"Google API rate limit exceeded: {e.message}")
            match = re.search(r"Please retry in (\d+\.?\d*)s", str(e.message))
            if match:
                delay_seconds = float(match.group(1)) + 1
                logger.info(f"API instructed to wait for {delay_seconds:.1f} seconds. Complying...")
                time.sleep(delay_seconds)
            else:
                logger.warning("Could not parse retry delay. Waiting for a standard 60 seconds.")
                time.sleep(60)
            return None # Return None to let the main loop's retry logic handle it
            
        except Exception as e:
            logger.error(f"Unexpected error during Gemini API call for tier {tier}: {e}")
            return None