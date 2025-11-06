import time
import uuid
import os
import concurrent.futures
import json
from datetime import datetime, timedelta
from core.context import rate_limiter, gemini_client, memory_manager, logger, status_update_queue
from core.dmn import creative_synthesis_loop, generate_eod_summary
from core.planner import orchestrate_planning
from core.tools import TOOL_EXECUTOR, TOOL_MANIFEST
from core.executor import run_executor
from utils.database import get_active_goal, update_goal, archive_goal
from pydantic import BaseModel, Field
from typing import Dict, Any

# --- Configuration ---
MAX_RETRIES = 2
IDLE_THRESHOLD_SECONDS = 300 # 5 minutes
REACT_MAX_ITERATIONS = 10 # Max number of steps the ReAct loop can take

REACT_SCHEMA = {
    "type": "object",
    "properties": {
        "thought": {
            "type": "string",
            "description": "Your reasoning and plan for the next action."
        },
        "action": {
            "type": "object",
            "properties": {
                "tool_name": {"type": "string"},
                "parameters": {
                    "type": "string",
                    "description": "The parameters for the tool, as a single JSON-formatted string."
                }
            },
            "required": ["tool_name"]
        }
    },
    "required": ["thought", "action"]
}

# --- NEW: SCHEMA FOR THE 'finish' TOOL ---
# We will tell the ReAct agent to use a *different* schema for its final answer
# This bypasses the tier1 object bug for tool calls, but gives us
# a structured output for the final answer.
FINISH_SCHEMA = {
    "type": "object",
    "properties": {
        "answer_text": {
            "type": "string",
            "description": "A final summary of the work completed."
        },
        "answer_json": {
            "type": "array",
            "items": {"type": "object"},
            "description": "Any structured data (like a list of cases) to be returned to the main plan."
        }
    },
    "required": ["answer_text"]
}

def should_trigger_dmn(rate_limiter_instance, last_active_time: float) -> bool:
    """Determines if the DMN should trigger based on idle time and API capacity."""
    idle_duration = time.time() - last_active_time
    if idle_duration <= IDLE_THRESHOLD_SECONDS:
        return False
    t1_usage = rate_limiter_instance.get_daily_usage_percentage('tier1')
    t2_usage = rate_limiter_instance.get_daily_usage_percentage('tier2')
    if t1_usage < 80.0 or t2_usage < 80.0:
        logger.info(f"DMN trigger conditions met: Sustained idle ({int(idle_duration)}s) and surplus API capacity.")
        return True
    return False

def should_trigger_summary() -> bool:
    """Triggers a summary if the most recent one is more than 24 hours old."""
    report_dir = 'data/reports'
    os.makedirs(report_dir, exist_ok=True)
    summaries = [f for f in os.listdir(report_dir) if f.endswith('_summary.md')]
    if not summaries:
        logger.info("Daily briefing trigger: No previous summaries found. Generating first one.")
        return True
    latest_summary = sorted(summaries, reverse=True)[0]
    latest_summary_path = os.path.join(report_dir, latest_summary)
    last_mod_time = datetime.fromtimestamp(os.path.getmtime(latest_summary_path))
    if datetime.now() - last_mod_time > timedelta(hours=24):
        logger.info(f"Daily briefing trigger: Last summary was over 24 hours ago.")
        return True
    return False

def _execute_single_action(tool_call: dict, context_map: dict, active_goal: dict) -> str:
    """Executes a single tool call, using the goal's preferred tier."""
    tool_name = tool_call.get("tool_name")
    
    # --- THIS IS THE FIX ---
    # Parameters are now a direct dictionary, not a string.
    parameters_str = tool_call.get("parameters", "{}")
    parameters = {}
    if isinstance(parameters_str, str):
        try:
            parameters = json.loads(parameters_str)
        except json.JSONDecodeError:
            logger.error(f"Failed to decode parameters string: {parameters_str}")
            return "Error: Failed to decode parameters string."
    elif isinstance(parameters_str, dict):
        # Fallback for any old/lingering actions
        parameters = parameters_str
    # --- END FIX ---

    for key, value in parameters.items():
        if isinstance(value, str) and value in context_map:
            parameters[key] = context_map[value]

    if tool_name == "google_search":
        query = parameters.get("query", "")
        active_tier = active_goal.get('preferred_tier', 'tier1')
        
        # --- BUG FIX ---
        # The function is 'ask_gemini', not 'ask_get_gemini'
        response = gemini_client.ask_gemini(query, tier=active_tier, enable_search=True)
        # --- END BUG FIX ---

        if response == "RATE_LIMIT_HIT":
            return "RATE_LIMIT_HIT" 

        return response.text if response and hasattr(response, 'text') else "Error during search."
    elif tool_name in TOOL_EXECUTOR:
        return TOOL_EXECUTOR[tool_name](**parameters)
    else:
        return f"Error: Unknown tool '{tool_name}'"

def _run_react_loop(sub_goal: str, context_map: dict, active_goal: dict) -> str:
    """
    Runs the 'Smart Executor' (ReAct) loop using a manual schema
    and the goal's preferred tier.
    """
    logger.info(f"REACT_LOOP: Starting for sub-goal: '{sub_goal}'")
    
    history = []
    iteration = 0 
    
    active_tier = active_goal.get('preferred_tier', 'tier1')
    logger.info(f"REACT_LOOP: Using preferred tier: {active_tier}")
    
    # --- NEW: ADD FULL CONTEXT ---
    current_date_str = datetime.now().strftime("%A, %B %d, %Y")
    user_original_goal = active_goal.get('goal', 'No overall goal provided.')
    strategy_blueprint_str = json.dumps(active_goal.get('strategy_blueprint', {}), indent=2)
    # THIS IS THE NEW LINE YOU SUGGESTED:
    full_plan_str = json.dumps(active_goal.get('plan', []), indent=2)
    # --- END NEW CONTEXT ---
    
    while iteration < REACT_MAX_ITERATIONS:
        logger.info(f"REACT_LOOP: Iteration {iteration + 1}/{REACT_MAX_ITERATIONS}")

        history_str = "\n".join(history)
        
        # --- PROMPT IS NOW FULLY CONTEXT-AWARE ---
        prompt = f"""
        You are a ReAct (Reason+Act) agent, a specialized problem-solver. Your output MUST be a single, valid JSON object that adheres to the provided schema.

        **--- 1. YOUR HIGH-LEVEL MISSION ---**
        * **Current Date:** {current_date_str}
        * **User's Overall Goal:** "{user_original_goal}"
        * **Your Strategic Mandate:** {strategy_blueprint_str}

        **--- 2. THE FULL STRATEGIC PLAN ---**
        (This is the full blueprint, your current sub-goal is one part of it)
        {full_plan_str}

        **--- 3. YOUR CURRENT SUB-GOAL ---**
        "{sub_goal}"

        **--- 4. CONTEXT (DATA FROM PREVIOUS STEPS) ---**
        {json.dumps(context_map, indent=2)}

        **--- 5. HISTORY (YOUR PREVIOUS ATTEMPTS) ---**
        {history_str if history_str else "No actions taken yet."}

        **--- 6. AVAILABLE TOOLS ---**
        {json.dumps(TOOL_MANIFEST, indent=2)}

        **--- 7. CRITICAL INSTRUCTIONS (READ CAREFULLY) ---**
        1.  Your task is to execute the `CURRENT SUB-GOAL`.
        2.  Use the `HIGH-LEVEL MISSION` and `FULL STRATEGIC PLAN` to understand the *intent* and *depth* required. (e.g., "Am I researching for a final report, or just getting a single fact?").
        3.  If the sub-goal contains placeholders (e.g., `[output_of_step_X]`), you MUST find their values in the `CONTEXT` block.
        4.  If your sub-goal requires "recent" info, use your `Google Search` tool (it is currently {current_date_str}).
        5.  The 'parameters' field for any tool call MUST be a JSON-formatted STRING.
        6.  When your sub-goal is fully complete, you MUST call the `finish` tool and pass ALL your work (e.g., your full research summary) as a single string in the `answer` parameter. You MUST NOT use `write_to_file` unless the sub-goal explicitly tells you to.

        **--- 8. YOUR TASK ---**
        Now, provide your `thought` and `action` to execute the sub-goal.
        """
        
        response = gemini_client.ask_gemini(prompt, tier=active_tier, response_schema=REACT_SCHEMA)
        
        # ... (rest of the function is unchanged) ...
        
        if response == "RATE_LIMIT_HIT":
            logger.warning(f"REACT_LOOP: Internal rate limit was hit on {active_tier}.")
            
            if active_tier == 'tier1':
                logger.info("REACT_LOOP: Tier 1 limit hit. Escalating to user.")
                return "TIER_1_LIMIT_HIT" 
            else:
                logger.warning("REACT_LOOP: Pausing for 30 seconds before retrying.")
                history.append(f"Observation: Internal rate limit hit on {active_tier}. Pausing for 30s.")
                time.sleep(30)
                continue 
            
        elif not response or not hasattr(response, 'text') or not response.text:
            logger.error("REACT_LOOP: Received no text response from LLM (generic failure). Pausing for 5 seconds.")
            history.append("Observation: The API call failed for a non-rate-limit reason. Pausing briefly before retrying.")
            time.sleep(5)
            continue 
        
        try:
            decision = json.loads(response.text)
            thought = decision.get("thought")
            action = decision.get("action", {})
            tool_name = action.get("tool_name")
            
            history.append(f"Thought: {thought}")
            
            if tool_name == "finish":
                params = json.loads(action.get("parameters", "{}"))
                final_answer = params.get("answer", "Sub-goal achieved.")
                logger.info(f"REACT_LOOP: Finished successfully.")
                return final_answer 

            history.append(f"Action: Calling tool '{tool_name}' with parameters {action.get('parameters')}")
            
            observation = _execute_single_action(action, context_map, active_goal)
            history.append(f"Observation: {observation}")
            
            if observation == "RATE_LIMIT_HIT":
                if active_tier == 'tier1':
                    logger.info("REACT_LOOP: Tier 1 limit hit during tool execution. Escalating to user.")
                    return "TIER_1_LIMIT_HIT"
                else:
                    logger.warning("REACT_LOOP: Pausing for 30 seconds before retrying.")
                    history.append(f"Observation: Internal rate limit hit on {active_tier}. Pausing for 30s.")
                    time.sleep(30)
                    continue 
            
            iteration += 1

        except json.JSONDecodeError:
            logger.error(f"REACT_LOOP: Failed to decode JSON from a valid response.")
            history.append(f"Observation: The LLM returned a malformed JSON string. Raw text: {response.text}")
            iteration += 1 
        except Exception as e:
            logger.error(f"REACT_LOOP: Error processing a valid action. {e}")
            history.append(f"Observation: The action was valid but failed during execution. Error: {e}")
            iteration += 1 

        active_goal['execution_log'] = "\n".join(history)
        update_goal(active_goal)

    logger.warning("REACT_LOOP: Reached max iterations without 'finish' action. Sub-goal failed.")
    return None

def _execute_step(step: dict, goal: dict, context_map: dict) -> tuple[int, str | None]:
    """
    Helper function to execute a single step, using the goal's preferred tier.
    It now uses the Executor to refine all LLM inputs.
    """
    step_id = step.get('step_id')
    active_tier = goal.get('preferred_tier', 'tier1')

    try:
        if "tool_call" in step:
            tool_call = step["tool_call"]
            tool_name = tool_call.get("tool_name")
            parameters_str = tool_call.get("parameters", "{}")
            
            parameters = {}
            if isinstance(parameters_str, str):
                try:
                    parameters = json.loads(parameters_str)
                except json.JSONDecodeError:
                    logger.error(f"Step {step_id}: Failed to decode parameters string: {parameters_str}")
                    return step_id, "Error: Failed to decode parameters string."
            elif isinstance(parameters_str, dict):
                parameters = parameters_str

            # --- THIS IS THE NEW, CLEAN ROUTING LOGIC ---
            if tool_name == "reactive_solve":
                simple_sub_goal = parameters.get("sub_goal", "") 
                
                logger.info(f"EXECUTOR: Refining sub-goal for ReAct loop: '{simple_sub_goal}'")
                refined_sub_goal = run_executor(
                    user_goal=goal['goal'], full_plan=goal.get('plan', []), 
                    strategy_blueprint=goal.get('strategy_blueprint', {}),
                    context_map=context_map, current_step_prompt=simple_sub_goal,
                    gemini_client=gemini_client, task_type="refine_subgoal" # Specify task type
                )
                step['refined_prompt'] = refined_sub_goal # Save for dashboard
                
                logger.info(f"REACT_LOOP: Starting with refined sub-goal.")
                result = _run_react_loop(refined_sub_goal, context_map, goal)
                
                if result is None:
                    # ... (failure handling) ...
                    logger.warning(f"Step {step_id} (reactive_solve) failed. Returning execution log.")
                    error_output = goal.get('execution_log')
                    if not error_output:
                        error_output = "ReAct loop failed and returned None. Execution log was empty."
                    return step_id, error_output
                else:
                    return step_id, result
            
            # --- THIS IS THE NEW LOGIC BLOCK ---
            elif tool_name == "google_search":
                simple_query = parameters.get("query", "")
                
                logger.info(f"EXECUTOR: Refining google_search query: '{simple_query}'")
                refined_query = run_executor(
                    user_goal=goal['goal'], full_plan=goal.get('plan', []),
                    strategy_blueprint=goal.get('strategy_blueprint', {}),
                    context_map=context_map, current_step_prompt=simple_query,
                    gemini_client=gemini_client, task_type="refine_query" # Specify task type
                )
                step['refined_prompt'] = refined_query # Save for dashboard
                
                # Re-build the tool_call with the refined query
                refined_tool_call = {
                    "tool_name": "google_search",
                    "parameters": json.dumps({"query": refined_query})
                }
                return step_id, _execute_single_action(refined_tool_call, context_map, goal)

            elif tool_name == "request_user_input":
                return step_id, "AWAITING_USER_INPUT_SIGNAL"
            
            else:
                # This handles write_to_file, draft_email, etc. (no refinement)
                return step_id, _execute_single_action(tool_call, context_map, goal)
            # --- END NEW ROUTING LOGIC ---
        
        elif "prompt" in step:
            simple_prompt = step.get('prompt', '')

            refined_prompt = run_executor(
                user_goal=goal['goal'], full_plan=goal.get('plan', []), 
                strategy_blueprint=goal.get('strategy_blueprint', {}),
                context_map=context_map, current_step_prompt=simple_prompt,
                gemini_client=gemini_client, task_type="refine_prompt" # Specify task type
            )
            
            step['refined_prompt'] = refined_prompt 
            should_search = "google_search" in refined_prompt.lower()
            
            logger.info(f"Step {step_id}: Executing prompt using tier: {active_tier}")
            response_obj = gemini_client.ask_gemini(refined_prompt, tier=active_tier, enable_search=should_search)
            
            # ... (rest of prompt execution logic) ...
            if response_obj == "RATE_LIMIT_HIT":
                if active_tier == 'tier1':
                    logger.info(f"Step {step_id}: Tier 1 limit hit. Escalating to user.")
                    return step_id, "TIER_1_LIMIT_HIT"
                else:
                    logger.warning(f"Step {step_id}: Rate limit hit on {active_tier}. Will retry.")
                    return step_id, "RATE_LIMIT_HIT"

            if response_obj and hasattr(response_obj, 'text'):
                return step_id, response_obj.text
            else:
                return step_id, None

    except Exception as e:
        logger.error(f"Error executing step {step_id} in a worker thread: {e}")
        return step_id, None

def main():
    """The main orchestrator loop, with Swarm, ReAct, and Tier Fallback."""
    logger.info("--- ⚙️ Orchestrator v6.3 Initializing (Re-Planning) ---")
    last_active_time = time.time()
    
    while True:
        logger.info("\n--- Orchestrator waking up... ---")
        active_goal = get_active_goal()
        
        if active_goal:
            last_active_time = time.time()
            
            # --- NEW RE-PLANNING LOGIC ---
            if active_goal.get('status') == 'pending' and not active_goal.get('plan'):
                logger.info(f"Found pending goal '{active_goal['goal_id']}' with no plan. Sending to planner...")
                
                # Get the tier from the goal (which the user may have set to tier2)
                current_tier = active_goal.get('preferred_tier', 'tier1')
                
                # Re-run the planning process
                new_goal_obj = orchestrate_planning(active_goal['goal'], preferred_tier=current_tier)
                
                if new_goal_obj:
                    # Update the existing goal with the new data
                    active_goal.update(new_goal_obj)
                    
                    if new_goal_obj.get('status') == 'pending':
                        active_goal['status'] = 'in-progress' # Start work immediately
                    
                    update_goal(active_goal)
                    status_update_queue.put("goal_updated")
                    logger.info("Goal successfully re-planned and updated.")
                
                else:
                    # Planning failed
                    logger.error(f"Re-planning failed for goal '{active_goal['goal_id']}'. Archiving as failed.")
                    active_goal['status'] = 'failed'
                    update_goal(active_goal)
                    archive_goal(active_goal['goal_id'])
                    status_update_queue.put("goal_updated")
                
                # Continue to the next loop iteration
                time.sleep(1) # Short sleep before re-evaluating
                continue 
            # --- END NEW RE-PLANNING LOGIC ---
                
            if active_goal.get('status') == 'pending' and active_goal.get('plan'):
                active_goal['status'] = 'in-progress'
            
            completed_step_ids = {s['step_id'] for s in active_goal['plan'] if s['status'] == 'complete'}
            
            executable_steps = [s for s in active_goal['plan'] if s['status'] == 'pending' and set(s.get('dependencies', [])).issubset(completed_step_ids)]
            
            if not executable_steps:
                if all(s['status'] != 'pending' for s in active_goal['plan']):
                    final_status = 'complete' if all(s['status'] == 'complete' for s in active_goal['plan']) else 'failed'
                    active_goal['status'] = final_status
                    logger.info(f"All steps processed for goal '{active_goal['goal_id']}'. Final status: {final_status}. Archiving.")
                    update_goal(active_goal)
                    archive_goal(active_goal['goal_id'])
                    status_update_queue.put("goal_updated") 
                else:
                    logger.info("-> No executable steps found (waiting for dependencies).")
                
            else:
                # ... (rest of your swarm/heavyweight task logic) ...
                # (This part of your main function does not need to change)
                heavyweight_step = next((step for step in executable_steps if step.get("tool_call", {}).get("tool_name") == "reactive_solve"), None)
                
                if heavyweight_step:
                    logger.info(f"Prioritizing heavyweight task: Step {heavyweight_step.get('step_id')}. Executing exclusively.")
                    context_map = {f"[output_of_step_{s.get('step_id')}]": s.get('output', '') for s in active_goal['plan'] if s['status'] == 'complete'}
                    step_id, response = _execute_step(heavyweight_step, active_goal, context_map)
                    
                    if response == "TIER_1_LIMIT_HIT":
                        active_goal['status'] = 'awaiting_tier_decision'
                        logger.warning(f"Heavyweight Step {step_id} hit Tier 1 limit. Pausing goal for user input.")
                    elif response == "RATE_LIMIT_HIT":
                         logger.warning(f"Heavyweight Step {step_id} hit Tier 2 limit. Will retry on next cycle.")
                    elif response:
                        heavyweight_step['output'] = response
                        heavyweight_step['status'] = 'complete'
                        logger.info(f"Heavyweight Step {step_id} completed successfully.")    
                    else:
                        heavyweight_step['output'] = "ReAct loop failed or returned None."
                        heavyweight_step['status'] = 'failed'
                        active_goal['status'] = 'failed'
                        logger.error(f"Heavyweight Step {step_id} failed permanently. Halting and archiving goal.")
                        update_goal(active_goal)
                        archive_goal(active_goal['goal_id'])
                        status_update_queue.put("goal_updated")
                        
                else:
                    logger.info(f"Found a swarm of {len(executable_steps)} executable steps. Dispatching...")
                    context_map = {f"[output_of_step_{s.get('step_id')}]": s.get('output', '') for s in active_goal['plan'] if s['status'] == 'complete'}

                    with concurrent.futures.ThreadPoolExecutor(max_workers=len(executable_steps)) as executor:
                        future_to_step = {executor.submit(_execute_step, step, active_goal, context_map): step for step in executable_steps}
                        
                        for future in concurrent.futures.as_completed(future_to_step):
                            step = future_to_step[future]
                            step_id, response = future.result()
                            
                            if response == "AWAITING_USER_INPUT_SIGNAL":
                                active_goal['status'] = 'awaiting_input'
                                logger.info(f"Step {step_id} is now awaiting user input. Goal paused.")
                            
                            elif response == "TIER_1_LIMIT_HIT":
                                active_goal['status'] = 'awaiting_tier_decision'
                                logger.warning(f"Step {step_id} hit Tier 1 limit. Pausing goal for user input.")
                                break 
                            elif response == "RATE_LIMIT_HIT":
                                logger.warning(f"Step {step_id} hit Tier 2 limit. Will retry on next cycle.")
                                
                            elif response:
                                step['output'] = response
                                step['status'] = 'complete'
                                logger.info(f"Step {step_id} completed successfully in swarm.")
                            else:
                                retries = step.get('retries', 0)
                                if retries < MAX_RETRIES:
                                    step['retries'] = retries + 1
                                    logger.warning(f"Step {step_id} failed in swarm. Will retry (attempt {retries + 1}/{MAX_RETRIES})...")
                                else:
                                    step['output'] = f"Step failed after {MAX_RETRIES + 1} attempts."
                                    step['status'] = 'failed'
                                    active_goal['status'] = 'failed'
                                    logger.error(f"Step {step_id} failed permanently. Halting and archiving goal.")
                                    update_goal(active_goal)
                                    archive_goal(active_goal['goal_id'])
                                    status_update_queue.put("goal_updated") 
                                    break
                
                if active_goal['status'] != 'failed':
                    update_goal(active_goal)
                    status_update_queue.put("goal_updated")

        elif should_trigger_summary():
            logger.info("\n>>> Daily Briefing Triggered...")
            generate_eod_summary(memory_manager, gemini_client)
            status_update_queue.put("goal_updated") 
        else:
            logger.info("-> No active goals found. Standing by.")
            
        logger.info("--- Orchestrator going to sleep for 30 seconds. ---")
        time.sleep(30)