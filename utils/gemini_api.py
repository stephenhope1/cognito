import os
import re
import time
from google import genai
from google.genai import types
from google.genai import errors as genai_errors
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
        self.code_execution_tool = types.Tool(
            code_execution=types.ToolCodeExecution()
        )
        self.maps_tool = types.Tool(
            google_maps=types.GoogleMaps()
        )

    def ask_gemini(self, 
                   prompt: str | list,
                   tier: str, 
                   generation_config: dict = None,
                   tools: list = None,
                   enable_search: bool = False, 
                   enable_code_execution: bool = False,
                   enable_maps: bool = False,
                   response_schema=None, 
                   system_instruction: str = None
                   ) -> types.GenerateContentResponse | None | str:
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

        # This is our *internal* (minute) rate limiter
        if not self.rate_limiter.check_and_increment(tier):
            logger.warning(f"API call to {tier} blocked by internal rate limiter (minute).")
            return "RATE_LIMIT_HIT" # Signal for a short retry

        try:
            model_name = self.model_map[tier]
            
            # 1. Prepare the tools list
            final_tools_list = tools if tools else []
            if enable_search:
                logger.info("Executing prompt with Google Search enabled.")
                final_tools_list.append(self.grounding_tool)
            if enable_code_execution:
                logger.info("Executing prompt with Code Execution enabled.")
                final_tools_list.append(self.code_execution_tool)
            if enable_maps:
                logger.info("Executing prompt with Google Maps enabled.")
                final_tools_list.append(self.maps_tool)

            # 2. Prepare the final configuration dictionary
            final_config_dict = generation_config.copy() if generation_config else {}
            
            if "thinkingBudget" in final_config_dict:
                budget = final_config_dict.pop("thinkingBudget")
                logger.info(f"Applying thinking budget: {budget}")
                final_config_dict['thinking_config'] = types.ThinkingConfig(
                    thinking_budget=budget
                )

            if response_schema:
                logger.info("Executing prompt with structured output schema.")
                final_config_dict['response_mime_type'] = "application/json"
                final_config_dict['response_schema'] = response_schema

            if system_instruction:
                logger.info("Applying system instruction.")
                final_config_dict['system_instruction'] = system_instruction
            
            if final_tools_list:
                final_config_dict['tools'] = final_tools_list

            final_config_object = None
            if final_config_dict:
                final_config_object = types.GenerateContentConfig(
                    **final_config_dict
                )

            # 4. Make the API call
            response = self.client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=final_config_object,
            )
            
            return response
            
        # We now catch *both* 429 and 503 errors and just return None.
        
        # This catches 429 QUOTA errors
        except genai_errors.ClientError as e:
            logger.warning(f"Google API QUOTA limit exceeded (429): {e.message}")
            return None # Return None to trigger a retry in main.py
        
        # This catches 503 SERVER OVERLOAD errors
        except genai_errors.ServerError as e:
            logger.warning(f"Google API Server Error (503). Treating as retryable: {e.message}")
            # Return RATE_LIMIT_HIT to trigger the retry/downgrade logic in the orchestrator
            return "RATE_LIMIT_HIT"
            
        except Exception as e:
             logger.error(f"Unexpected error during Gemini API call for tier {tier}: {e}", exc_info=True)