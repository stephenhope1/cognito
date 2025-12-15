import json
import time
from datetime import datetime
from typing import Optional, List, Dict, Any, Union
from pydantic import BaseModel, Field, ValidationError

from core.context import logger, gemini_client, memory_manager
from .strategist import run_strategist
from .tools import TOOL_MANIFEST
from .agent_profile import get_agent_profile 
from utils.database import get_user_profile

MAX_PLANNING_RETRIES = 1

AGENT_PROFILE_FOR_PLANNER = get_agent_profile(for_planner=True)

# --- MODERN PYDANTIC DEFINITIONS ---
class ToolCall(BaseModel):
    """
    Represents a tool invocation within a plan step.
    """
    tool_name: str = Field(..., description="The name of the tool to be executed.")
    parameters: Dict[str, Any] = Field(default_factory=dict, description="Key-value arguments for the tool.")

class PlanStep(BaseModel):
    """
    Represents a single step in the execution plan.
    Each step must either contain a 'prompt' (for pure LLM tasks) or a 'tool_call' (for actions).
    """
    step_id: int = Field(..., description="Unique identifier for this step.")
    dependencies: List[int] = Field(default_factory=list, description="IDs of steps that must complete before this one.")
    prompt: Optional[str] = Field(None, description="LLM instruction for this step (if no tool is used).")
    tool_call: Optional[ToolCall] = Field(None, description="The tool to execute for this step (if applicable).")

class Plan(BaseModel):
    """
    Container for the full list of steps.
    """
    plan: List[PlanStep]

def validate_plan(plan_data: list) -> tuple[bool, str | None]:
    """
    Validates the structure of the generated plan using Pydantic models.

    Args:
        plan_data: The raw list of dictionaries returned by the LLM.

    Returns:
        tuple: (is_valid: bool, error_message: str|None)
    """
    logger.info("VALIDATOR: Validating plan structure...")
    
    try:
        # Validate the list of steps against our Pydantic model
        validated = Plan(plan=plan_data)
        
        # Additional Logic Checks
        for i, step in enumerate(validated.plan):
            # Mutual exclusion check: Prompt vs Tool Call
            if step.prompt and step.tool_call:
                return False, f"Step {step.step_id} cannot have both 'prompt' and 'tool_call'."
            if not step.prompt and not step.tool_call:
                return False, f"Step {step.step_id} must have either 'prompt' or 'tool_call'."
            
            # Tool existence check
            if step.tool_call:
                tool_name = step.tool_call.tool_name
                # 'reactive_solve' is a meta-tool handled by the Executor, not in TOOL_MANIFEST
                if tool_name != "reactive_solve":
                    if not any(t['tool_name'] == tool_name for t in TOOL_MANIFEST):
                        return False, f"Unknown tool: {tool_name}"

        logger.info("VALIDATOR: Plan structure is valid.")
        return True, None
        
    except ValidationError as e:
        logger.error(f"VALIDATION ERROR: {e}")
        return False, str(e)
    except Exception as e:
        return False, f"Unexpected validation error: {e}"

def orchestrate_planning(user_goal: str, preferred_tier: str = 'tier1', existing_context_str: str = None) -> dict | None:
    """
    The main entry point for the Planning Phase.

    Workflow:
    1. Run the Strategist to determine the approach (cognitive gear).
    2. If clarification is needed, return a plan requesting user input.
    3. Generate the step-by-step plan using the LLM (with retries).
    4. Validate the plan structure.

    Args:
        user_goal: The raw goal string from the user.
        preferred_tier: The API tier to use (tier1/tier2).
        existing_context_str: Optional context from previous failures (for replanning).

    Returns:
        dict: The final goal object including the plan and metadata, or None if failed.
    """
    logger.info(f"PLANNING ORCHESTRATOR: Starting planning for goal: '{user_goal}'")

    # 1. Run Strategist
    strategy_blueprint = run_strategist(user_goal)
    if not strategy_blueprint: return None

    # 2. Handle Clarification (Pre-Planning)
    if strategy_blueprint.requires_clarification and not existing_context_str:
        clarification_plan = [{
            "step_id": 1, "dependencies": [],
            "tool_call": {
                "tool_name": "request_user_input",
                "parameters": {"question": strategy_blueprint.clarification_question}
            }, "status": "pending", "output": None
        }]
        return {
            "goal": user_goal, "plan": clarification_plan, "status": "awaiting_input",
            "audit_critique": "Awaiting user clarification.",
            "strategy_blueprint": strategy_blueprint.model_dump(), "preferred_tier": preferred_tier
        }

    plan_json = None
    retry_context = None
    rate_limit_retries = 0
    retry_count = 0
    is_valid = False

    # 3. Planning Loop with Retries
    while retry_count <= MAX_PLANNING_RETRIES:
        plan_json = generate_plan(user_goal, strategy_blueprint.model_dump(), gemini_client, retry_context, preferred_tier, existing_context_str)
        
        # Handle API Rate Limits
        if plan_json == "RATE_LIMIT_HIT":
            rate_limit_retries += 1
            time.sleep(30)
            if rate_limit_retries > MAX_PLANNING_RETRIES: return None
            continue

        # Handle Empty/Failed Generation
        if not plan_json:
            retry_count += 1
            retry_context = {"previous_invalid_plan": "None", "validation_error": "Planner returned empty/invalid JSON."}
            if retry_count > MAX_PLANNING_RETRIES: break 
            continue

        # Validate Generated Plan
        is_valid, validation_error = validate_plan(plan_json)
        
        if is_valid: break

        # Prepare for Retry if Invalid
        retry_count += 1
        retry_context = {"previous_invalid_plan": plan_json, "validation_error": validation_error}

    if is_valid:
        # Normalize the plan steps (convert Pydantic models to dicts)
        clean_plan = [step.model_dump() if hasattr(step, 'model_dump') else step for step in Plan(plan=plan_json).plan]

        # Return the structured Goal Object
        return {
            "goal": user_goal,
            "plan": [{**step, "status": "pending", "output": None} for step in clean_plan],
            "audit_critique": f"Plan generated using '{strategy_blueprint.cognitive_gear}' gear.",
            "status": "pending",
            "strategy_blueprint": strategy_blueprint.model_dump(),
            "preferred_tier": preferred_tier
        }
    
    logger.error("PLANNING ORCHESTRATOR: Failed to generate a valid plan.")
    return None

def generate_plan(user_goal: str, strategy_blueprint: dict, gemini_client, retry_context: dict = None, tier: str = 'tier1', existing_context_str: str = None) -> list | None | str:
    """
    Generates the raw JSON plan from the LLM based on strategy and context.
    """
    # Use response_mime_type="application/json" for forced JSON output.
    planner_generation_config = {
        "temperature": 0.1,
        "response_mime_type": "application/json" 
    }
    
    current_date_str = datetime.now().strftime("%A, %B %d, %Y")
    
    # Construct Context Strings
    retry_str = ""
    if retry_context:
        retry_str = f"**RETRY CONTEXT:** Previous error: {retry_context.get('validation_error')}. Fix the plan structure."

    context_str = ""
    if existing_context_str:
        context_str = f"**RE-PLANNING CONTEXT:**\n{existing_context_str}"
        
    # Heuristics Injection (RAG)
    heuristic_prompt_addition = ""
    try:
        heuristics = memory_manager.find_similar_memories(query_text=user_goal, n_results=3, where_filter={"type": "heuristic"})
        if heuristics:
            heuristics_str = "\n".join(f"- {h}" for h in heuristics)
            heuristic_prompt_addition = f"**RELEVANT HEURISTICS:**\n{heuristics_str}"
    except Exception: pass

    # User Profile Injection
    user_profile_addition = ""
    try:
        profile = get_user_profile()
        if profile:
            profile_str = json.dumps(profile, indent=2)
            user_profile_addition = f"**USER PROFILE:**\n{profile_str}"
    except Exception: pass

    system_instruction = AGENT_PROFILE_FOR_PLANNER
    
    # Final Prompt Assembly
    prompt = f"""
    {retry_str}
    **TASK:** Create a JSON plan for: "{user_goal}"
    **STRATEGY:** {json.dumps(strategy_blueprint, indent=2)}
    **DATE:** {current_date_str}
    
    {context_str}
    {heuristic_prompt_addition}
    {user_profile_addition}
    
    **FINAL INSTRUCTION:** Return ONLY the raw JSON object. 
    The JSON must follow the structure: {{ "plan": [ ... steps ... ] }}
    """
    
    try:
        response = gemini_client.ask_gemini(
            prompt, tier=tier, generation_config=planner_generation_config,
            # response_schema=None, # We rely on text parsing for maximum flexibility here
            system_instruction=system_instruction
        )
        
        if response == "RATE_LIMIT_HIT": return "RATE_LIMIT_HIT"
        
        if response and response.text:
            try:
                parsed = json.loads(response.text)
                
                # Robustly handle different top-level JSON structures
                if isinstance(parsed, list):
                    # The model returned the list directly (e.g. [step1, step2])
                    return parsed
                elif isinstance(parsed, dict):
                    # The model returned the wrapper (e.g. {"plan": [step1, step2]})
                    return parsed.get('plan', [])
                else:
                    logger.error(f"PLANNER: Unexpected JSON structure: {type(parsed)}")
                    return None
                    
            except json.JSONDecodeError:
                logger.error("PLANNER: Failed to decode JSON response.")
                return None
        
    except Exception as e:
        logger.error(f"PLANNER GEN ERROR: {e}")
        
    return None
