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
    cognitive_gear: str = Field(
        description=(
            "Select the single best cognitive gear based on the user's goal and the following mandates:\n"
            
            "1. 'Direct_Response':\n"
            "   - **Use Case:** Simple, factual, or conversational queries that can be answered directly.\n"
            "   - **Mandate:** You MUST create the shortest, most direct plan possible (usually 1-2 steps). You are FORBIDDEN from adding any self-critique, verification, or other iterative steps.\n"
            
            "2. 'Reflective_Synthesis':\n"
            "   - **Use Case:** Queries requiring summarizing, combining, or reformatting existing information or user context.\n"
            "   - **Mandate:** You MUST include at least ONE iterative step in your plan (e.g., a 'self-critique' or 'verification' prompt). You have the AUTONOMY to decide which kind of reflection is most appropriate for the task.\n"
            
            "3. 'Deep_Analysis':\n"
            "   - **Use Case:** Complex queries requiring multi-step reasoning, external tool use (like search), or in-depth problem-solving.\n"
            "   - **Mandate:** You are AUTHORIZED and EXPECTED to use advanced, multi-call techniques like multi-perspective analysis, red-teaming, or iterative refinement to ensure the highest possible quality. Your plan MUST reflect this level of diligence."
        )
    )

def run_strategist(user_goal: str) -> StrategyBlueprint | None:
    """
    Analyzes a user's goal and returns a structured StrategyBlueprint object
    by leveraging the Gemini API's structured output feature.
    """
    logger.info(f"STRATEGIST: Analyzing goal with 'Cognitive Gears' model: '{user_goal}'")

    current_date_str = datetime.now().strftime("%A, %B %d, %Y")

    system_instruction = """You are a hyper-efficient Triage AI. Your sole purpose is to analyze a user's goal and provide your analysis in the requested format. 
    Your internal knowledge is outdated. For any goal involving "recent events", assume the system's search tools will be used. Do not mark `requires_clarification` as `true` simply because a query is time-sensitive. 
    Based on the user's goal, provide your reasoning in the 'assessment' field and then select the appropriate cognitive gear and clarification status."
    """
    prompt = f"""

    **--- YOUR TASK ---**
    Analyze the following user goal and provide your strategic assessment.

    User Goal: "{user_goal}"
    Current Date: {current_date_str}.
    
    """

    # --- MODIFIED: Define the generation config for deterministic output ---
    strategist_generation_config = {
        "temperature": 0.1
    }
    # --- END MODIFIED ---

    # MODIFIED: We now pass our Pydantic class AND the new config to the API call.
    response = gemini_client.ask_gemini(
        prompt, 
        tier='tier2', 
        generation_config=strategist_generation_config, # <-- This is the change
        response_schema=StrategyBlueprint,
        system_instruction=system_instruction
    )

    # --- This is the block the error was about. It is now correctly indented. ---
    if not response or not hasattr(response, 'parsed'):
        logger.error("STRATEGIST: Failed to get a parsed response from the LLM.")
        logger.debug(f"Raw response text: {response.text if response else 'N/A'}")
        return None
        
    try:
        strategy = response.parsed
        if not isinstance(strategy, StrategyBlueprint):
            raise TypeError("Parsed response is not a StrategyBlueprint instance.")
            
        logger.info(f"STRATEGIST: Successfully generated strategy blueprint. Selected Gear: {strategy.cognitive_gear}")
        return strategy
    except Exception as e:
        logger.error(f"STRATEGIST: Error processing the parsed response. Reason: {e}")
        return None