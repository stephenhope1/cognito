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
    """
    A robust client for the modern Google GenAI SDK.

    Responsibilities:
    1. Managing API connections and authentication.
    2. Routing requests to the appropriate model tiers (Tier 1 = Pro, Tier 2 = Flash).
    3. Handling rate limits internally and catching Google SDK errors.
    4. Configuring advanced features like Search, Code Execution, and Structured Output.
    """
    
    def __init__(self, rate_limiter: RateLimitTracker):
        """Initializes the Gemini client."""
        try:
            load_dotenv()
            # Initialize the SDK Client (v1.55+)
            self.client = genai.Client()
            logger.info("Gemini API configured successfully using genai.Client.")
        except Exception as e:
            logger.fatal(f"Failed to configure Gemini API: {e}")
            self.client = None
            return

        self.rate_limiter = rate_limiter 

        # Map 'tier' to specific model names
        self.model_map = {
            'tier1': 'gemini-2.5-pro',
            'tier2': 'gemini-2.5-flash',
        }
        
        # Pre-configured native tool objects for performance
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

        Args:
            prompt: The input text or list of Content objects.
            tier: 'tier1' (Pro) or 'tier2' (Flash).
            generation_config: Dict of config params (temperature, etc.).
            tools: List of custom tool definitions.
            enable_search: Bool to toggle Google Search grounding.
            enable_code_execution: Bool to toggle Python sandbox.
            enable_maps: Bool to toggle Google Maps.
            response_schema: Pydantic model or schema dict for structured JSON output.
            system_instruction: System prompt to set persona/behavior.

        Returns:
            - types.GenerateContentResponse: On success.
            - "RATE_LIMIT_HIT": If blocked by internal limiter or 503 error.
            - None: On fatal error or 429 quota exhaustion.
        """
        if not self.client:
            logger.error("Gemini client not initialized.")
            return None
            
        if tier not in self.model_map:
            logger.error(f"Invalid tier '{tier}'.")
            return None

        # Check Internal Minute-Rate Limiter
        if not self.rate_limiter.check_and_increment(tier):
            logger.warning(f"API call to {tier} blocked by internal rate limiter (minute).")
            return "RATE_LIMIT_HIT" # Signal for a short retry

        try:
            model_name = self.model_map[tier]
            
            # 1. Prepare Tools List
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

            # 2. Prepare Config Object
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

            # 3. Execute API Call
            response = self.client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=final_config_object,
            )
            
            return response
            
        # Error Handling Strategy:
        
        # 429: Quota Exceeded (Daily Limit)
        # We return None to tell the Orchestrator to potentially pause or switch tiers (if logic existed).
        except genai_errors.ClientError as e:
            logger.warning(f"Google API QUOTA limit exceeded (429): {e.message}")
            return None
        
        # 503: Server Overload (Temporary)
        # We return RATE_LIMIT_HIT to trigger a short sleep and retry loop in the Orchestrator.
        except genai_errors.ServerError as e:
            logger.warning(f"Google API Server Error (503). Treating as retryable: {e.message}")
            return "RATE_LIMIT_HIT"
            
        except Exception as e:
             logger.error(f"Unexpected error during Gemini API call for tier {tier}: {e}", exc_info=True)
             return None
