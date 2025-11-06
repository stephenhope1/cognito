import json
from datetime import datetime
from typing import Optional
from enum import Enum
from pydantic import BaseModel, Field

from core.context import logger, gemini_client

# This Pydantic model IS the blueprint we will give to the API.
class StrategyBlueprint(BaseModel):
    assessment: str = Field(description="A brief, one-sentence justification for the strategic choices.")
    requires_clarification: bool = Field(description="True if the user's goal is ambiguous and needs more information.")
    clarification_question: Optional[str] = Field(description="The specific question to ask the user if clarification is needed, otherwise null.")
    cognitive_gear: str = Field(description="One of ['Direct_Response', 'Reflective_Synthesis', 'Deep_Analysis']")

def run_strategist(user_goal: str) -> StrategyBlueprint | None:
    """
    Analyzes a user's goal and returns a structured StrategyBlueprint object
    by leveraging the Gemini API's structured output feature.
    """
    logger.info(f"STRATEGIST: Analyzing goal with 'Cognitive Gears' model: '{user_goal}'")

    current_date_str = datetime.now().strftime("%A, %B %d, %Y")

    # MODIFIED: The prompt is now much simpler. It focuses on the task,
    # not the formatting, because the API will handle the formatting.
    prompt = f"""
    You are a hyper-efficient Triage AI. Your sole purpose is to analyze a user's goal and provide your analysis in the requested format.

    **--- CRITICAL CONTEXT & INSTRUCTIONS ---**
    1.  **Current Date:** The current date is {current_date_str}.
    2.  **Temporal Awareness:** Your internal knowledge is outdated. For any goal involving "recent events", assume the system's search tools will be used. Do not mark `requires_clarification` as `true` simply because a query is time-sensitive.
    3.  **Analysis:** Based on the user's goal, provide your reasoning in the 'assessment' field and then select the appropriate cognitive gear and clarification status.

    **--- YOUR TASK ---**
    Analyze the following user goal and provide your strategic assessment.

    User Goal: "{user_goal}"
    """

    # MODIFIED: We now pass our Pydantic class directly to the API call.
    response = gemini_client.ask_gemini(
        prompt, 
        tier='tier2', 
        response_schema=StrategyBlueprint
    )

    if not response or not hasattr(response, 'parsed'):
        logger.error("STRATEGIST: Failed to get a parsed response from the LLM.")
        logger.debug(f"Raw response text: {response.text if response else 'N/A'}")
        return None
        
    try:
        # MODIFIED: No more manual JSON parsing! We directly access the parsed object.
        strategy = response.parsed
        if not isinstance(strategy, StrategyBlueprint):
            raise TypeError("Parsed response is not a StrategyBlueprint instance.")
            
        logger.info(f"STRATEGIST: Successfully generated strategy blueprint. Selected Gear: {strategy.cognitive_gear}")
        return strategy
    except Exception as e:
        logger.error(f"STRATEGIST: Error processing the parsed response. Reason: {e}")
        return None