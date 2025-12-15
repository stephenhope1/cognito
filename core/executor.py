from core.context import logger
import json
from pydantic import BaseModel, Field
from typing import Literal, Union, List, Optional
from .agent_profile import get_agent_profile

# --- Structured Task Definition ---
class ExecutorTaskSpec(BaseModel):
    """
    Defines the structured output of the Executor.
    This tells the ReAct loop exactly how to 'Hot Start' the task.
    """
    primary_tool: Literal["google_search", "get_maps_data", "execute_python_code", "none"] = Field(
        description="The single best tool to start this task. Use 'none' if no tool is needed immediately."
    )
    initial_inputs: List[str] = Field(
        description="A list of specific, optimized queries or code snippets to run immediately. For search, this is the query string."
    )
    task_description: str = Field(
        description="A concise, imperative instruction for the ReAct agent on what to do with the tool output."
    )

AGENT_PROFILE = get_agent_profile(for_planner=False)

# --- Few-Shot Examples for Prompting ---
SUBGOAL_EXAMPLES = """
**EXAMPLE 1: Research**
Input: "Find out the capital of Canada."
Output: {
  "primary_tool": "google_search",
  "initial_inputs": ["current capital city of Canada"],
  "task_description": "Extract the capital city from the search results and state it clearly."
}

**EXAMPLE 2: Maps**
Input: "Find coffee near Eiffel Tower."
Output: {
  "primary_tool": "get_maps_data",
  "initial_inputs": ["coffee shops near Eiffel Tower"],
  "task_description": "List the top 3 coffee shops found, including ratings and addresses."
}

**EXAMPLE 3: Code**
Input: "Calculate 28% of 5,482."
Output: {
  "primary_tool": "execute_python_code",
  "initial_inputs": ["print(5482 * 0.28)"],
  "task_description": "Run the calculation and provide the numeric answer."
}
"""

QUERY_EXAMPLES = """ 
--- EXAMPLE --- 
TASK: Refine the simple query below. SIMPLE QUERY: "recent supreme court cases 2025"
YOUR OUTPUT: ("Supreme Court of Canada" OR SCC) AND (judgments OR decisions) AND 2025
"""

def run_executor(user_goal: str, full_plan: list, strategy_blueprint: dict, context_map: dict, current_step_prompt: str, gemini_client, task_type: str) -> Union[str, dict, ExecutorTaskSpec]: 
    """
    The Executor's role is to refine a high-level plan step into a concrete, executable instruction.

    Modes:
    1. refine_prompt: Simply rewrites a text prompt to be more expert-like.
    2. refine_subgoal: Converts a goal into an `ExecutorTaskSpec` (Tool + Input + Instruction).
    3. refine_query: Optimizes a search string for Google.

    Args:
        user_goal: The overall user objective.
        full_plan: The complete list of steps.
        strategy_blueprint: The strategy object from the Planner.
        context_map: A dictionary of {source: content} for available context.
        current_step_prompt: The raw instruction from the plan.
        gemini_client: The LLM client.
        task_type: One of 'refine_prompt', 'refine_subgoal', 'refine_query'.

    Returns:
        Union[str, ExecutorTaskSpec]: either refined text or a structured object.
    """
    logger.info(f"EXECUTOR: Refining task (type: {task_type}) for step: '{current_step_prompt}'")

    # Serialize context for the prompt
    context_str = "\n".join([f"CONTEXT FOR `{k}`: {v}" for k, v in context_map.items()])
    plan_str = json.dumps(full_plan, indent=2)

    executor_generation_config = {"temperature": 0.1} # Lower temp for structure reliability
    response_schema = None
    persona = ""
    task_instructions = ""

    # --- Prompt Construction based on Task Type ---
    if task_type == "refine_prompt":
        persona = "You are the 'Executor'. Rewrite the simple step into an expert prompt."
        task_instructions = f"Refine this step: '{current_step_prompt}'"
        
    elif task_type == "refine_subgoal":
        persona = f"""
        You are the 'Task Architect'. Your job is to convert a high-level sub-goal into a concrete execution plan.
        
        **CRITICAL INSTRUCTION:**
        Do NOT write a long prompt. Instead, identify the ONE primary tool needed to start this task and provide the exact input (query/code) for it.
        
        {AGENT_PROFILE}
        
        **YOUR TASK:**
        1. Analyze the sub-goal.
        2. Select the `primary_tool`.
        3. Write the `initial_inputs` (search queries or code).
        4. Write a `task_description` for the analysis phase.
        
        {SUBGOAL_EXAMPLES}
        """
        response_schema = ExecutorTaskSpec
        task_instructions = f"""
        **TASK: Architect the execution for this sub-goal.**
        **CURRENT SUB-GOAL:**
        "{current_step_prompt}"
        """
        
    elif task_type == "refine_query":
        persona = "You are a 'Search Query Expert'. Output ONLY the optimized search string."
        task_instructions = f"Optimize this query: '{current_step_prompt}'"

    final_prompt = f"""
    **CONTEXT (FOR YOUR USE ONLY)**
    **The User's Overall Goal:** "{user_goal}"
    **The Full Strategic Plan:** {plan_str}
    **Resolved Context:** {context_str[:2000] if context_str else "None"}
    ---
    {task_instructions}
    """

    # --- LLM Call ---
    response = gemini_client.ask_gemini(
        final_prompt,
        tier='tier2',
        generation_config=executor_generation_config,
        response_schema=response_schema,
        system_instruction=persona
    )

    # --- Response Handling ---
    if task_type == "refine_subgoal":
        if response and hasattr(response, 'parsed'):
            logger.info("EXECUTOR: Successfully generated TaskSpec.")
            return response.parsed # Returns ExecutorTaskSpec object
        else:
            logger.warning("EXECUTOR: Failed to generate TaskSpec. Returning default fallback.")
            # Fallback: Treat as a generic task with no immediate tool
            return ExecutorTaskSpec(
                primary_tool="none", 
                initial_inputs=[], 
                task_description=current_step_prompt
            )
    else:
        # For text-based refinements
        if response == "RATE_LIMIT_HIT": return current_step_prompt
        if response and response.text: return response.text.strip()
        return current_step_prompt
