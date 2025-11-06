import json
import time
from datetime import datetime
from typing import Optional, List, Dict, Any, Union
from pydantic import BaseModel, Field

from core.context import logger, gemini_client
from .strategist import run_strategist
from .tools import TOOL_MANIFEST

MAX_PLANNING_RETRIES = 1

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "plan": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "step_id": {"type": "integer"},
                    "dependencies": {
                        "type": "array",
                        "items": {"type": "integer"}
                    },
                    "prompt": {
                        "type": "string",
                        "description": "A prompt for the Executor. Use this for analysis or generation."
                    },
                    "tool_call": {
                        "type": "object",
                        "properties": {
                            "tool_name": {"type": "string"},
                            "parameters": {
                                "type": "string",  # CHANGED from "object"
                                "description": "The parameters for the tool, as a single JSON-formatted string."
                            }
                        },
                        "description": "A call to a specific tool. Use this for actions."
                    }
                },
                "required": ["step_id", "dependencies"],
            }
        }
    },
    "required": ["plan"]
}

GEAR_MANDATES = {
    "Direct_Response": "You MUST create the shortest, most direct plan possible (usually 1-2 steps). You are FORBIDDEN from adding any self-critique, verification, or other iterative steps.",
    "Reflective_Synthesis": "You MUST include at least ONE iterative step in your plan (e.g., a 'self-critique' or 'verification' prompt). You have the AUTONOMY to decide which kind of reflection is most appropriate for the task.",
    "Deep_Analysis": "You are AUTHORIZED and EXPECTED to use advanced, multi-call techniques like multi-perspective analysis, red-teaming, or iterative refinement to ensure the highest possible quality. Your plan MUST reflect this level of diligence."
}

def _force_allow_additional_properties(schema: dict) -> dict:
    """
    Recursively traverses a JSON schema and adds 'additionalProperties': True
    to every object definition that doesn't already have it.
    This is a workaround for the Gemini API's strict schema parser.
    """
    if not isinstance(schema, dict):
        return schema

    if schema.get("type") == "object" and "additionalProperties" not in schema:
        schema["additionalProperties"] = True

    for key, value in schema.items():
        if isinstance(value, dict):
            _force_allow_additional_properties(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _force_allow_additional_properties(item)
    return schema

def validate_plan(plan: list) -> tuple[bool, str | None]:
    """
    Programmatically validates the structure of a generated plan.
    Returns (True, None) on success, or (False, error_message) on failure.
    """
    logger.info("VALIDATOR: Validating plan structure...")
    error_message = None

    SPECIAL_TOOLS = ["google_search", "request_user_input", "reactive_solve"]

    if not isinstance(plan, list) or not plan:
        error_message = "Plan is not a non-empty list."
    else:
        for i, step in enumerate(plan):
            if not isinstance(step, dict):
                error_message = f"Step {i+1} is not a dictionary."
                break
            if 'step_id' not in step or 'dependencies' not in step:
                error_message = f"Step {i+1} is missing 'step_id' or 'dependencies'."
                break
            has_prompt = 'prompt' in step and step['prompt'] is not None
            has_tool_call = 'tool_call' in step and step['tool_call'] is not None
            if not (has_prompt ^ has_tool_call):
                error_message = f"Step {i+1} must have exactly one of 'prompt' or 'tool_call'."
                break
            if has_tool_call:
                tool_call = step['tool_call']
                if not isinstance(tool_call, dict) or 'tool_name' not in tool_call or 'parameters' not in tool_call:
                    error_message = f"Step {i+1} has a malformed 'tool_call' object."
                    break
                tool_name = tool_call['tool_name']
                tool_in_manifest = any(t['tool_name'] == tool_name for t in TOOL_MANIFEST)
                if not tool_in_manifest and tool_name not in SPECIAL_TOOLS:
                    error_message = f"Step {i+1} references an unknown tool: '{tool_name}'."
                    break

    if error_message:
        logger.error(f"VALIDATOR ERROR: {error_message}")
        return False, error_message
    else:
        logger.info("VALIDATOR: Plan structure is valid.")
        return True, None

def orchestrate_planning(user_goal: str, preferred_tier: str = 'tier1') -> dict | None:
    """
    Orchestrates the full Strategist -> Planner -> Validator pipeline.
    If tier1 is rate-limited, it creates a goal in 'awaiting_tier_decision' state.
    """
    logger.info(f"PLANNING ORCHESTRATOR: Starting planning for goal: '{user_goal}' using tier: {preferred_tier}")

    # Strategist is fast and cheap, we can leave it on tier2
    strategy_blueprint = run_strategist(user_goal)
    if not strategy_blueprint:
        logger.error("PLANNING ORCHESTRATOR: Strategist failed. Aborting.")
        return None

    if strategy_blueprint.requires_clarification:
        logger.info("PLANNING ORCHESTRATOR: Strategy requires user input. Pausing goal.")
        clarification_plan = [{
            "step_id": 1, "dependencies": [],
            "tool_call": {
                "tool_name": "request_user_input",
                "parameters": json.dumps({"question": strategy_blueprint.clarification_question})
            }, "status": "pending", "output": None
        }]
        return {
            "goal": user_goal, "plan": clarification_plan,
            "status": "awaiting_input",
            "audit_critique": "Awaiting user clarification before full planning.",
            "strategy_blueprint": strategy_blueprint.model_dump(),
            "preferred_tier": preferred_tier
        }

    plan_json = None
    retry_context = None
    retry_count = 0
    is_valid = False

    while retry_count <= MAX_PLANNING_RETRIES:
        plan_json = generate_plan(
            user_goal=user_goal,
            strategy_blueprint=strategy_blueprint.model_dump(),
            gemini_client=gemini_client,
            retry_context=retry_context,
            tier=preferred_tier # Pass the selected tier
        )
        
        if plan_json == "RATE_LIMIT_HIT":
            if preferred_tier == 'tier1':
                # --- THIS IS THE NEW LOGIC ---
                logger.warning("PLANNING ORCHESTRATOR: Tier 1 rate limit hit. Creating goal to ask user.")
                # Return a "dummy" goal that can be re-planned later
                return {
                    "goal": user_goal,
                    "plan": [], # Empty plan
                    "status": "awaiting_tier_decision",
                    "audit_critique": "Planning paused due to Tier 1 rate limit.",
                    "strategy_blueprint": strategy_blueprint.model_dump(),
                    "preferred_tier": 'tier1'
                }
                # --- END NEW LOGIC ---
            else:
                # You're on tier2, so just wait and retry (the original loop)
                logger.warning(f"PLANNING ORCHESTRATOR: Rate limit hit on {preferred_tier}. Pausing for 30s...")
                time.sleep(30)
                continue # Try again without incrementing retry_count

        if not plan_json:
            logger.error(f"PLANNING ORCHESTRATOR: Planner failed to generate any plan (Attempt {retry_count + 1}).")
            retry_count += 1
            retry_context = {"previous_invalid_plan": "None", "validation_error": "Planner returned an empty plan."}
            time.sleep(5) 
            continue

        is_valid, validation_error = validate_plan(plan_json)
        
        if is_valid:
            logger.info(f"PLANNING ORCHESTRATOR: Plan validated successfully (Attempt {retry_count + 1}).")
            break

        retry_count += 1
        if retry_count <= MAX_PLANNING_RETRIES:
            logger.warning(f"PLANNING ORCHESTRATOR: Plan failed validation. Error: {validation_error}. Retrying ({retry_count}/{MAX_PLANNING_RETRIES})...")
            retry_context = {"previous_invalid_plan": plan_json, "validation_error": validation_error}
        else:
            logger.error(f"PLANNING ORCHESTRATOR: Plan failed validation after {MAX_PLANNING_RETRIES + 1} attempts. Aborting.")
            return None

    if is_valid:
        return {
            "goal": user_goal,
            "plan": [{**step, "status": "pending", "output": None} for step in plan_json],
            "audit_critique": f"Plan generated using '{strategy_blueprint.cognitive_gear}' gear.",
            "status": "pending",
            "strategy_blueprint": strategy_blueprint.model_dump(),
            "preferred_tier": preferred_tier
        }
    else:
        return None

def generate_plan(user_goal: str, strategy_blueprint: dict, gemini_client, retry_context: dict = None, tier: str = 'tier1') -> list | None | str:
    """
    Generates a detailed, step-by-step JSON plan using a manual schema.
    Returns a plan list, None on failure, or "RATE_LIMIT_HIT" on rate limit.
    """
    retry_prompt_addition = ""
    if retry_context:
        logger.info("PLANNER: Attempting to regenerate plan based on validation feedback...")
        retry_prompt_addition = f"""
        **--- IMPORTANT: RETRY CONTEXT ---**
        Your previous attempt failed validation with the error: "{retry_context.get('validation_error')}"
        Here is the invalid plan you generated: {json.dumps(retry_context.get('previous_invalid_plan'), indent=2)}
        You MUST generate a NEW, CORRECTED plan that fixes this specific issue.
        **--- END RETRY CONTEXT ---**
        """
    else:
        logger.info("PLANNER: Generating initial plan based on strategy blueprint...")

    strategy_json_str = json.dumps(strategy_blueprint, indent=2)
    tools_json_str = json.dumps(TOOL_MANIFEST, indent=2)
    cognitive_gear = strategy_blueprint.get("cognitive_gear", "Direct_Response")
    current_date_str = datetime.now().strftime("%A, %B %d, %Y")
    mandate = GEAR_MANDATES.get(cognitive_gear, GEAR_MANDATES["Direct_Response"])

    prompt = f"""
    You are a "Strategic Plan Architect," a hyper-disciplined AI component in a larger agentic framework.
    Your *only* task is to create a detailed, step-by-step JSON plan. You DO NOT execute the steps or write the final answer. You build the blueprint.
    Your output MUST be only the plan's raw JSON.

    {retry_prompt_addition}

    **--- CRITICAL CONTEXT ---**
    1.  **Your Role:** You are the Architect. You design the plan for other agents to execute.
    2.  **The Executor:** A "Tactical" AI (the Executor) will refine all `prompt` steps.
    3.  **The ReAct Agent:** A "Problem Solver" (the ReAct loop) will execute all `reactive_solve` tool calls.
    4.  **The Swarm:** The system can execute steps in parallel.

    **--- YOUR TASK ---**
    Create a step-by-step plan based on the provided inputs.
    
    **--- CRITICAL SCHEMA NOTE ---**
    The 'parameters' field for any 'tool_call' MUST be a JSON-formatted STRING.
    
    **FOR MOST TOOLS** (like `Google Search` or `finish`):
    Example: "parameters": "{{\"query\": \"capital of Australia\"}}"

    **EXCEPTION: `write_to_file` TOOL**
    To avoid escaping errors, you MUST use this exact heredoc-style format.
    The `content` must be a single JSON string, with newlines as `\\n`.
    
    Example:
    "parameters": "{{\"filename\": \"report.md\", \"content\": \"Line 1 of the report.\\nLine 2 of the report.\\n- A bullet point\\n\"}}"
    ---

    **--- INPUTS ---**
    **INPUT 1: The User's Original Goal:** "{user_goal}"
    **INPUT 2: The Strategy Blueprint (Your Mandate):** {strategy_json_str}
    **INPUT 3: Your Available Tools:** {tools_json_str}
    **INPUT 4: Current Date:** "{current_date_str}"
    
    **YOUR MANDATE FOR THIS PLAN IS:**
    **{mandate}**

   **--- 4. PLANNER'S CORE DIRECTIVES (READ CAREFULLY) ---**

    **1. AGENTIC BEHAVIOR:**
        * **Temporal Awareness:** Your internal knowledge is outdated. It is currently {current_date_str}. You **MUST** use `Google Search` or `reactive_solve` for any query about "recent" events.
        * **Confidence:** Act as a confident expert. Do **NOT** use `request_user_input` to ask for confirmation.

    **2. TOOL SELECTION LOGIC (CRITICAL):**
        * **Use `Google Search` (For Simple Facts):** Use this for simple, one-shot, factual lookups (e.g., "What is the capital of Australia?", "Find a quick list of 2025 SCC cases"). The Executor will automatically refine your query.
        * **Use `reactive_solve` (For Deep Research & Complex Tasks):** Use this for *all* deep research, analysis, extraction, or multi-step tasks (e.g., "Research the *implications* of R. v. Morrison," "Analyze [output_of_step_1] and extract the case names").
        * **Use `prompt` (For Synthesis):** Use this ONLY for *combining* the outputs of previous steps (e.g., "Write a blog post using [output_of_step_2]").
        * **Use `write_to_file` (For Final Output):** This should be the *last* step of a plan.

    **3. THE DECONSTRUCTION MANDATE (CRITICAL):**
        * If a goal requires iterating over a list (e.g., "research all 5 cases"), you **MUST** create a "map/reduce" plan:
            * **MAP:** Create a *separate*, parallel `reactive_solve` step for *each item*.
            * **REDUCE:** Create a *final* `prompt` step that synthesizes all the parallel outputs.

    **4. PLAN STRUCTURE:**
        * **Dependencies:** If steps can run at the same time, they MUST have the same dependencies.
        * **Placeholders:** Use `[output_of_step_X]` to reference outputs.
    ---

    **--- EXAMPLES OF EXPECTED OUTPUT ---**

    **Example for `cognitive_gear: "Direct_Response"`:**
    (Goal: "What is the capital of Australia?")
    ```json
    [
      {{
        "step_id": 1,
        "dependencies": [],
        "tool_call": {{
          "tool_name": "google_search",
          "parameters": {{
            "query": "capital of Australia"
          }}
        }}
      }}
    ]
    ```

    **Example for `cognitive_gear: "Reflective_Synthesis"`:**
    (Goal: "Write a blog post about the benefits of hydration.")
    ```json
    [
      {{
        "step_id": 1,
        "dependencies": [],
        "prompt": "Using google_search, research the top five scientifically-backed benefits of staying hydrated."
      }},
      {{
        "step_id": 2,
        "dependencies": [1],
        "prompt": "Based on the research from [output_of_step_1], write a 300-word blog post titled 'The Clear Benefits of Hydration'. This is the first draft."
      }},
      {{
        "step_id": 3,
        "dependencies": [2],
        "prompt": "Critically review the first draft from [output_of_step_2]. Check for clarity, engagement, and factual accuracy. Output your findings as a bulleted list of suggested improvements."
      }},
      {{
        "step_id": 4,
        "dependencies": [3],
        "prompt": "Revise the blog post from [output_of_step_2] by incorporating the suggested improvements from [output_of_step_3] to create a final, polished version."
      }}
    ]
    ```

    **Example for `cognitive_gear: "Deep_Analysis"` (Using `reactive_solve`):**
    (Goal: "Research the top 3 SCC cases from 2025 and write a summary report.")
    ```json
    [
        {{
          "step_id": 1,
          "dependencies": [],
          "tool_call": {{
            "tool_name": "google_search",
            "parameters": "{{\"query\": \"top Supreme Court of Canada cases 2025\"}}"
          }}
        }},
        {{
          "step_id": 2,
          "dependencies": [1],
          "tool_call": {{
            "tool_name": "reactive_solve",
            "parameters": "{{\"sub_goal\": \"From the search results in [output_of_step_1], identify the *first* most significant case. Then, conduct in-depth research on this single case and return a full summary.\"}}"
          }}
        }},
        {{
          "step_id": 3,
          "dependencies": [1],
          "tool_call": {{
            "tool_name": "reactive_solve",
            "parameters": "{{\"sub_goal\": \"From the search results in [output_of_step_1], identify the *second* most significant case. Then, conduct in-depth research on this single case and return a full summary.\"}}"
          }}
        }},
        {{
          "step_id": 4,
          "dependencies": [1],
          "tool_call": {{
            "tool_name": "reactive_solve",
            "parameters": "{{\"sub_goal\": \"From the search results in [output_of_step_1], identify the *third* most significant case. Then, conduct in-depth research on this single case and return a full summary.\"}}"
          }}
        }},
        {{
          "step_id": 5,
          "dependencies": [2, 3, 4],
          "prompt": "Act as a senior legal scholar. Synthesize the three detailed case summaries from [output_of_step_2], [output_of_step_3], and [output_of_step_4] into a single, comprehensive report. This is the first draft."
        }},
        {{
          "step_id": 6,
          "dependencies": [5],
          "prompt": "Act as a skeptical legal editor. Perform a 'red-team' critique of the report from [output_of_step_5]. Check for logical inconsistencies, weak arguments, or missed analytical connections between the cases. Output a bulleted list of actionable improvements."
        }},
        {{
          "step_id": 7,
          "dependencies": [5, 6],
          "prompt": "Act as the original legal scholar. Revise the first draft report from [output_of_step_5] by meticulously incorporating all the actionable improvements from the critique in [output_of_step_6]. Produce the final, polished version of the report."
        }},
        {{
          "step_id": 8,
          "dependencies": [7],
          "tool_call": {{
            "tool_name": "write_to_file",
            "parameters": "{{\"filename\": \"SCC_Report_2025.md\", \"content\": \"[output_of_step_7]\"}}"
          }}
        }}
    ]
    ```

    **--- FINAL INSTRUCTION ---**
    Now, generate the JSON object for the plan. Your output MUST be only the JSON.
    """
    
    response = gemini_client.ask_gemini(
        prompt, 
        tier=tier, 
        response_schema=PLAN_SCHEMA
    )
    
    if response == "RATE_LIMIT_HIT":
        logger.warning("PLANNER: Rate limit hit during plan generation.")
        return "RATE_LIMIT_HIT"
    
    if not response or not hasattr(response, 'text') or not response.text:
        logger.error("PLANNER: Failed to get any response from the LLM.")
        logger.debug(f"Raw response: {response}")
        return None
        
    try:
        if not response or not hasattr(response, 'parsed') or not response.parsed:
            logger.error("PLANNER: Failed to get a parsed plan from the LLM.")
            logger.debug(f"Raw response text: {response.text if response else 'N/A'}")
            return None

        plan_data = response.parsed
        logger.info(f"PLANNER: Successfully generated a plan with {len(plan_data.get('plan', []))} steps.")
        return plan_data.get('plan')
    except Exception as e:
        logger.error(f"PLANNER: Error processing the parsed plan. Reason: {e}")
        return None