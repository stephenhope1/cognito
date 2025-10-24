import json
from core.context import logger
from core.context import gemini_client
from .tools import TOOL_MANIFEST
from .strategist import run_strategist

MAX_PLANNING_RETRIES = 1

def validate_plan(plan: list) -> tuple[bool, str | None]:
    """
    Programmatically validates the structure of a generated plan.
    Returns (True, None) on success, or (False, error_message) on failure.
    """
    logger.info("VALIDATOR: Validating plan structure...")
    error_message = None

    if not isinstance(plan, list):
        error_message = "Plan is not a list."
    else:
        for i, step in enumerate(plan):
            if not isinstance(step, dict):
                error_message = f"Step {i+1} is not a dictionary."
                break
            
            if 'step_id' not in step or 'dependencies' not in step:
                error_message = f"Step {i+1} is missing 'step_id' or 'dependencies'."
                break
                
            has_prompt = 'prompt' in step
            has_tool_call = 'tool_call' in step
            if not (has_prompt ^ has_tool_call):
                error_message = f"Step {i+1} must have exactly one of 'prompt' or 'tool_call'."
                break
                
            if has_tool_call:
                tool_call = step['tool_call']
                if not isinstance(tool_call, dict) or 'tool_name' not in tool_call or 'parameters' not in tool_call:
                    error_message = f"Step {i+1} has a malformed 'tool_call' object."
                    break
                
                tool_name = tool_call['tool_name']
                tool_exists = any(t['tool_name'] == tool_name for t in TOOL_MANIFEST)
                if not tool_exists and tool_name != "google_search": # Allow google_search implicitly
                    error_message = f"Step {i+1} references an unknown tool: '{tool_name}'."
                    break
            # Add more validation rules here if needed in the future

    if error_message:
        logger.error(f"VALIDATOR ERROR: {error_message}")
        return False, error_message
    else:
        logger.info("VALIDATOR: Plan structure is valid.")
        return True, None

def orchestrate_planning(user_goal: str) -> dict | None:
    """
    Orchestrates the full Strategist -> Planner -> Validator pipeline,
    including a retry loop for validation failures.
    """
    logger.info(f"PLANNING ORCHESTRATOR: Starting planning cycle for goal: '{user_goal}'")

    # 1. Run Strategist
    strategy_blueprint = run_strategist(user_goal)
    if not strategy_blueprint:
        logger.error("PLANNING ORCHESTRATOR: Strategist failed to generate a blueprint. Aborting.")
        return None

    # 2. Handle Clarification
    if strategy_blueprint.get("requires_clarification"):
        logger.info("PLANNING ORCHESTRATOR: Strategy requires user input. Pausing goal.")
        clarification_plan = [{
            "step_id": 1,
            "dependencies": [],
            "tool_call": {
                "tool_name": "request_user_input",
                "parameters": {"question": strategy_blueprint.get("clarification_question")}
            },
            "status": "pending", "output": None
        }]
        return {
            "goal": user_goal,
            "plan": clarification_plan,
            "status": "awaiting_input",
            "audit_critique": "Awaiting user clarification before full planning."
        }

    # 3. Initial Plan Generation & Retry Loop
    plan_json = None
    retry_context = None
    retry_count = 0
    is_valid = False
    validation_error = None

    while retry_count <= MAX_PLANNING_RETRIES:
        plan_json = generate_plan(
            user_goal=user_goal,
            strategy_blueprint=strategy_blueprint,
            gemini_client=gemini_client,
            retry_context=retry_context
        )
        
        if not plan_json:
             logger.error(f"PLANNING ORCHESTRATOR: Planner failed to generate any plan (Attempt {retry_count + 1}).")
             retry_count += 1
             retry_context = {
                 "previous_invalid_plan": "None",
                 "validation_error": "Planner returned an empty plan."
             }
             continue # Go to the next iteration of the loop to retry

        is_valid, validation_error = validate_plan(plan_json)
        
        if is_valid:
            logger.info(f"PLANNING ORCHESTRATOR: Plan validated successfully (Attempt {retry_count + 1}).")
            break

        # If invalid and retries remain:
        retry_count += 1
        if retry_count <= MAX_PLANNING_RETRIES:
            logger.warning(f"PLANNING ORCHESTRATOR: Plan failed validation. Error: {validation_error}. Retrying ({retry_count}/{MAX_PLANNING_RETRIES})...")
            # Set up the context for the next retry
            retry_context = {
                "previous_invalid_plan": plan_json,
                "validation_error": validation_error
            }
        else:
            logger.error(f"PLANNING ORCHESTRATOR: Plan failed validation after {MAX_PLANNING_RETRIES + 1} attempts. Aborting.")
            return None # Abort planning after final retry fails

    # 4. Construct the final goal object (only if valid)
    if is_valid:
        new_goal = {
            "goal": user_goal,
            "plan": [{**step, "status": "pending", "output": None} for step in plan_json],
            "audit_critique": f"Plan generated using '{strategy_blueprint.get('cognitive_gear')}' gear.",
            "status": "pending"
        }
        return new_goal
    else:
        return None

def generate_plan(user_goal: str, strategy_blueprint: dict, gemini_client, retry_context: dict = None) -> list | None:
    """
    Generates a detailed, step-by-step JSON plan that includes dependencies.
    Can accept retry_context to attempt correcting a previously invalid plan.
    """
    retry_prompt_addition = ""
    if retry_context:
        logger.info("PLANNER: Attempting to regenerate plan based on validation feedback...")
        retry_prompt_addition = f"""
        **--- IMPORTANT: RETRY CONTEXT ---**
        Your previous attempt to generate a plan failed validation with the following error:
        "{retry_context.get('validation_error')}"

        Here is the invalid plan you generated previously:
        ```json
        {json.dumps(retry_context.get('previous_invalid_plan'), indent=2)}
        ```
        You MUST analyze this error and generate a NEW, CORRECTED plan that fixes the specific validation issue while still adhering to all original rules and the strategy blueprint.
        **--- END RETRY CONTEST ---**
        """
    else:
        logger.info("PLANNER: Generating initial plan based on strategy blueprint...")

    strategy_json_str = json.dumps(strategy_blueprint, indent=2)
    tools_json_str = json.dumps(TOOL_MANIFEST, indent=2)
    cognitive_gear = strategy_blueprint.get("cognitive_gear", "Direct_Response")

    prompt = f"""
    You are a hyper-disciplined AI Planner, a specialized component in a larger agentic framework. Your output will be programmatically parsed to create a dependency graph for execution.

    {retry_prompt_addition}

    **--- CRITICAL CONTEXT & ROLE ---**
    1.  **Your Role:** Generate a detailed, step-by-step plan as a single JSON array, correctly identifying the `dependencies` for each step based on logical flow.
    2.  **Consequences of Failure:** The system relies on a perfect JSON structure and a logical dependency graph. Errors will cause the entire agent's workflow to fail.

    **--- YOUR TASK ---**
    Create a step-by-step plan based on the provided inputs.

    **INPUT 1: The User's Original Goal:** "{user_goal}"
    **INPUT 2: The Strategy Blueprint (Your Mandate):** {strategy_json_str}
    **INPUT 3: Your Available Tools:** {tools_json_str}

    **--- CORE PLANNING PRINCIPLES ---**
    1.  **Adhere to Mandate:** The plan's depth and inclusion of critique steps MUST align with the `cognitive_gear` from the Strategy Blueprint.
    2.  **Logical Dependencies:** Your primary objective is to define the correct `dependencies` for each step. A step's dependencies are the `step_id`s that must be complete before it can begin. Steps that don't depend on each other should have the same or empty dependencies.
    3.  **Completeness:** Every plan must end with a final, actionable step that delivers the result to the user (e.g., a `tool_call` to `write_to_file` or `draft_email`).
    4.  **Reference Outputs:** Use the placeholder `[output_of_step_X]` to reference outputs from previous steps.

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
        "prompt": "Based on the research from [output_of_step_1], write a 300-word blog post titled 'The Clear Benefits of Hydration'."
      }},
      {{
        "step_id": 3,
        "dependencies": [2],
        "prompt": "Critically review the blog post from [output_of_step_2]. Check for clarity, engagement, and factual accuracy. Output your findings as a bulleted list of suggested improvements."
      }},
      {{
        "step_id": 4,
        "dependencies": [3],
        "prompt": "Revise the blog post from [output_of_step_2] by incorporating the suggested improvements from [output_of_step_3] to create a final, polished version."
      }}
    ]
    ```

    **Example for `cognitive_gear: "Deep_Analysis"`:**
    (Goal: "Develop a marketing strategy for a new coffee brand.")
    ```json
    [
      {{
        "step_id": 1,
        "dependencies": [],
        "tool_call": {{ "tool_name": "request_user_input", "parameters": {{"question": "What is the new coffee brand's name, target audience, and unique selling proposition?"}} }}
      }},
      {{
        "step_id": 2,
        "dependencies": [1],
        "prompt": "Act as a Market Analyst. Based on the user's input from [output_of_step_1], use google_search to research the competitive landscape."
      }},
      {{
        "step_id": 3,
        "dependencies": [1],
        "prompt": "Act as a Brand Strategist. Based on the user's input from [output_of_step_1], define a compelling brand voice."
      }},
      {{
        "step_id": 4,
        "dependencies": [2, 3],
        "prompt": "Synthesize the competitive analysis from [output_of_step_2] and the brand strategy from [output_of_step_3] into a single, cohesive marketing strategy document. This is the first draft."
      }},
      {{
        "step_id": 5,
        "dependencies": [4],
        "prompt": "Act as a 'Red Team' critic. Relentlessly critique the first draft of the marketing strategy from [output_of_step_4]. Identify the single biggest assumption or weakest point in the plan and explain why it might fail."
      }},
      {{
        "step_id": 6,
        "dependencies": [5],
        "prompt": "Act as the original strategist. Based on the highly critical feedback from [output_of_step_5], rewrite the first draft from [output_of_step_4] to address all identified weaknesses and produce a final, more robust version of the marketing strategy."
      }},
      {{
        "step_id": 7,
        "dependencies": [6],
        "tool_call": {{ "tool_name": "write_to_file", "parameters": {{"filename": "final_marketing_strategy.md", "content": "[output_of_step_6]"}} }}
      }}
    ]
    ```

    **--- FINAL INSTRUCTION ---**
    Now, generate the JSON plan with a logical dependency graph for the provided user goal, adhering strictly to all rules.
    """
    
    # Planning is a critical reasoning task, so we always use the top-tier model.
    response_text = gemini_client.ask_gemini(prompt, tier='tier1')
    
    if not response_text:
        logger.error("PLANNER: Failed to get a response from the LLM.")
        return None
        
    try:
        json_start = response_text.find('[')
        json_end = response_text.rfind(']')
        if json_start != -1 and json_end != -1:
            clean_json_str = response_text[json_start : json_end + 1]
            plan = json.loads(clean_json_str)
            logger.info(f"PLANNER: Successfully generated and parsed a {len(plan)}-step dependency graph using '{cognitive_gear}' gear.")
            return plan
        else:
            raise json.JSONDecodeError("Could not find a JSON array in the response.", response_text, 0)
    except json.JSONDecodeError as e:
        logger.error(f"PLANNER: Could not parse JSON from the agent's response. Reason: {e}")
        logger.error(f"Received: {response_text}")
        return None