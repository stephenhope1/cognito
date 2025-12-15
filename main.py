import time
import uuid
import os
import concurrent.futures
import json
from datetime import datetime, timedelta
from typing import Dict, Any

# Internal Core Imports
from core.context import rate_limiter, gemini_client, memory_manager, logger, status_update_queue, orchestrator_wake_event
from core.dmn import generate_eod_summary, run_dmn_tasks
from core.planner import orchestrate_planning
from core.tools import TOOL_EXECUTOR, TOOL_MANIFEST
from core.executor import run_executor, ExecutorTaskSpec
from core.context_curator import ContextCurator
from utils.database import get_active_goal, update_goal, archive_goal, add_goal, get_recent_failed_goals, get_goal_status_by_id, get_goal_by_id

# Google GenAI Imports
from google.genai import types
from pydantic import BaseModel, Field

# --- System Configuration ---
MAX_RETRIES = 2
IDLE_THRESHOLD_SECONDS = 300 
REACT_MAX_ITERATIONS = 10 

# =================================================================================================
# HELPER FUNCTIONS
# =================================================================================================

def should_trigger_dmn(rate_limiter_instance, last_active_time: float) -> bool:
    """
    Checks if the Default Mode Network (background dreaming/cleanup) should run.
    Triggered if system is idle AND we have surplus API quota.
    """
    idle_duration = time.time() - last_active_time
    if idle_duration <= IDLE_THRESHOLD_SECONDS: return False 
    try:
        t1_usage_pct = rate_limiter_instance.get_daily_usage_percentage('tier1')
        t2_usage_pct = rate_limiter_instance.get_daily_usage_percentage('tier2')
        time_elapsed_pct = rate_limiter_instance.get_time_elapsed_percentage()

        # Calculate surplus (Time Elapsed % - Usage %)
        t1_surplus = time_elapsed_pct - t1_usage_pct
        t2_surplus = time_elapsed_pct - t2_usage_pct

        DMN_TRIGGER_THRESHOLD = 25.0 
        if t1_surplus > DMN_TRIGGER_THRESHOLD or t2_surplus > DMN_TRIGGER_THRESHOLD:
            logger.info(f"DMN trigger conditions met. API Surplus available.")
            return True
        return False 
    except Exception as e:
        logger.error(f"Error in should_trigger_dmn logic: {e}")
        return False

def should_trigger_summary() -> bool:
    """
    Checks if it's time to generate the End-of-Day summary.
    Triggered once every 24 hours.
    """
    report_dir = 'data/reports'
    os.makedirs(report_dir, exist_ok=True)
    summaries = [f for f in os.listdir(report_dir) if f.endswith('_summary.md')]
    if not summaries: return True

    latest_summary = sorted(summaries, reverse=True)[0]
    latest_summary_path = os.path.join(report_dir, latest_summary)
    last_mod_time = datetime.fromtimestamp(os.path.getmtime(latest_summary_path))

    if datetime.now() - last_mod_time > timedelta(hours=24): return True
    return False

def _add_citations_to_response(response: types.GenerateContentResponse) -> str:
    """
    Extracts grounding metadata from Google Search responses and injects markdown citations.
    """
    try:
        if not response.candidates: return response.text
        candidate = response.candidates[0]
        metadata = candidate.grounding_metadata
        if not metadata or not metadata.grounding_supports: return response.text

        text = response.text
        supports = metadata.grounding_supports
        chunks = metadata.grounding_chunks

        # Process supports in reverse order to avoid index shifts when inserting text
        sorted_supports = sorted(supports, key=lambda s: s.segment.end_index, reverse=True)

        for support in sorted_supports:
            end_index = support.segment.end_index
            citation_links = []
            if support.grounding_chunk_indices:
                for i in support.grounding_chunk_indices:
                    if i < len(chunks):
                        uri = chunks[i].web.uri
                        citation_links.append(f"[{i + 1}]({uri})")
            if citation_links:
                citation_string = ", ".join(citation_links)
                text = text[:end_index] + f" {citation_string}" + text[end_index:]
        return text
    except Exception as e:
        logger.error(f"Error processing citations: {e}")
        return response.text if response else ""
    
def _execute_single_action(tool_call: dict, context_map: dict, active_goal: dict) -> str:
    """
    Executes a standard tool (file I/O, email, etc.) that doesn't require the ReAct loop.
    Resolves parameter references from the context map.
    """
    tool_name = tool_call.get("tool_name")
    parameters_input = tool_call.get("parameters", {})
    parameters = {}

    # Parse parameters if they are a JSON string
    if isinstance(parameters_input, dict): parameters = parameters_input
    elif isinstance(parameters_input, str):
        try: parameters = json.loads(parameters_input)
        except json.JSONDecodeError: return "Error: Failed to decode parameters string."

    # Resolve Context Variables (e.g. "[output_of_step_1]")
    for key, value in parameters.items():
        if isinstance(value, str) and value in context_map: parameters[key] = context_map[value]

    if tool_name in TOOL_EXECUTOR:
        return TOOL_EXECUTOR[tool_name](**parameters)
    else:
        return f"Error: Unknown or mis-routed tool '{tool_name}'"

def _build_native_tools_list() -> list[types.Tool]:
    """
    Converts our TOOL_MANIFEST into the format required by the Google GenAI SDK.
    Skips 'reactive_solve' as it is a meta-tool.
    """
    native_tools = []
    NATIVE_TOOLS_SKIP_LIST = ["reactive_solve"]
    for tool_def in TOOL_MANIFEST:
        if tool_def['tool_name'] in NATIVE_TOOLS_SKIP_LIST: continue
        try:
            func = types.FunctionDeclaration(name=tool_def['tool_name'], description=tool_def['description'])
            schema_properties = {}
            required_params = []

            for param in tool_def.get('parameters', []):
                param_name = param['name']
                type_mapping = {"string": "STRING", "number": "NUMBER", "integer": "INTEGER"}
                schema_properties[param_name] = types.Schema(type=type_mapping.get(param['type'], "STRING"), description=param.get('description'))
                required_params.append(param_name)

            func.parameters = types.Schema(type="OBJECT", properties=schema_properties, required=required_params)
            native_tools.append(types.Tool(function_declarations=[func]))
        except Exception as e: logger.error(f"Error converting tool: {e}")
    return native_tools

def execute_native_tool(tool_name: str, tool_input: str, tier: str) -> str:
    """
    Centralized logic for executing Google Search, Code, and Maps.
    Handles API calls, error checking, and result parsing.
    """
    observation = None
    try:
        enable_s = (tool_name == "google_search")
        enable_c = (tool_name == "execute_python_code")
        enable_m = (tool_name == "get_maps_data")
        
        # Execute API Call
        response_obj = gemini_client.ask_gemini(
            tool_input, 
            tier=tier,
            enable_search=enable_s, enable_code_execution=enable_c, enable_maps=enable_m
        )
        
        # Parse Result
        if response_obj == "RATE_LIMIT_HIT":
            return "RATE_LIMIT_HIT"
        elif isinstance(response_obj, str):
            return response_obj # Pass through error strings
            
        if response_obj and hasattr(response_obj, 'text'):
            observation = _add_citations_to_response(response_obj) if enable_s else response_obj.text
            # Extract Code Output if available
            if enable_c and response_obj.candidates and response_obj.candidates[0].content.parts[0].code_execution_result:
                observation = response_obj.candidates[0].content.parts[0].code_execution_result.output
        else:
            observation = "Tool returned no text output."
            
    except Exception as e:
        logger.error(f"Error executing native tool {tool_name}: {e}")
        observation = f"Error executing tool: {e}"

    return observation

def _generate_step_summary(step_output: str) -> str:
    """
    Generates a concise 1-sentence summary of the step output for the Context Curator.
    This creates the "index" that allows for efficient retrieval later.
    """
    try:
        if not step_output: return "No output produced."
        if len(step_output) < 200: return step_output # Short enough already

        prompt = f"Summarize this output in one concise sentence: {step_output[:5000]}"
        response = gemini_client.ask_gemini(prompt, tier='tier2')

        if response and hasattr(response, 'text'):
            return response.text.strip()
        return step_output[:100] + "..."
    except Exception as e:
        logger.error(f"Error generating summary: {e}")
        return "Summary unavailable."

# =================================================================================================
# EXECUTION LOOPS
# =================================================================================================

def _run_react_loop_with_hot_start(task_spec: ExecutorTaskSpec, context_map: dict, active_goal: dict) -> str:
    """
    Runs the ReAct loop with a 'Hot Start' optimization.

    Concept:
    Instead of asking the LLM "What should I do?" and waiting for it to say "Search for X",
    the Executor has already determined "Search for X" is the best first move.
    We programmatically execute that move and feed the result into the LLM history *as if* it asked for it.
    This saves 1 full inference round-trip.
    """
    logger.info(f"REACT_LOOP: Hot Start initiated for task: {task_spec.task_description[:100]}...")
    
    goal_id = active_goal.get('goal_id')
    conversation_history = [] 
    all_tools_list = _build_native_tools_list()
    active_tier = active_goal.get('preferred_tier', 'tier1')
    react_generation_config = {"temperature": 0.1}
    
    # --- STEP 1: The "Hot Start" (Programmatic Execution) ---
    
    # A. Add the User's Goal (from Task Spec)
    initial_prompt = f"""
    **TASK:** {task_spec.task_description}
    **CONTEXT:** {json.dumps(context_map, indent=2)}
    """
    conversation_history.append(types.Content(role="user", parts=[types.Part(text=initial_prompt)]))
    
    # B. If a primary tool is defined, execute it immediately
    if task_spec.primary_tool != "none" and task_spec.initial_inputs:
        tool_name = task_spec.primary_tool
        tool_input = task_spec.initial_inputs[0] # Take the first input
        
        logger.info(f"REACT_LOOP: Hot-executing {tool_name} with input '{tool_input}'...")
        
        # B1. Inject "Assistant" turn (Simulating the LLM asking for the tool)
        conversation_history.append(types.Content(role="model", parts=[
            types.Part(function_call=types.FunctionCall(name=tool_name, args={"prompt": tool_input}))
        ]))
        
        # B2. Execute the tool directly
        observation = execute_native_tool(tool_name, tool_input, active_tier)

        # B3. Inject "Tool Output" turn
        conversation_history.append(types.Content(role="function", parts=[
            types.Part(function_response=types.FunctionResponse(name=tool_name, response={"content": observation}))
        ]))
        
        logger.info("REACT_LOOP: Hot Start complete. Handing control to LLM.")

    # --- STEP 2: The Standard ReAct Loop ---
    iteration = 0
    system_instruction = "You are a ReAct agent. Analyze the tool outputs provided in the history. If satisfied, output the final answer. If not, call another tool."

    while iteration < REACT_MAX_ITERATIONS:
        response = gemini_client.ask_gemini(
            conversation_history,
            tier=active_tier, 
            generation_config=react_generation_config,
            tools=all_tools_list, 
            system_instruction=system_instruction
        )
        
        if response == "RATE_LIMIT_HIT" or response is None: time.sleep(30); continue
        
        try:
            if not response.candidates:
                iteration += 1
                continue

            response_part = response.candidates[0].content.parts[0]

            if response_part.function_call:
                fc = response_part.function_call
                tool_name = fc.name
                tool_params = dict(fc.args)
                
                logger.info(f"REACT_LOOP: Calling tool '{tool_name}'")
                conversation_history.append(response.candidates[0].content)
                
                # Logic for inner loop execution
                # We can use the helper again if it's a native tool
                if tool_name in ["google_search", "get_maps_data", "execute_python_code"]:
                     q = tool_params.get("prompt") or tool_params.get("query")
                     obs = execute_native_tool(tool_name, q, active_tier)
                else:
                     # Fallback to standard execute step for non-native tools
                     _, obs = _execute_step({"tool_call": {"tool_name": tool_name, "parameters": tool_params}, "step_id": 0}, active_goal, context_map)
                
                conversation_history.append(types.Content(role="function", parts=[
                    types.Part(function_response=types.FunctionResponse(name=tool_name, response={"content": obs}))
                ]))

            elif response_part.text:
                return response_part.text
            
            iteration += 1
        except Exception as e:
            logger.error(f"REACT_LOOP: Error: {e}")
            iteration += 1 

    return "Max iterations reached."

def _execute_step(step: dict, goal: dict, context_map: dict) -> tuple[int, str | None]:
    """
    Executes a single step from the plan.
    Dispatches to either the ReAct loop (for complex tasks) or a single tool execution.
    """
    step_id = step.get('step_id')
    active_tier = goal.get('preferred_tier', 'tier1')
    
    try:
        if step.get("tool_call"):
            tool_call = step["tool_call"]
            tool_name = tool_call.get("tool_name")
            parameters = tool_call.get("parameters", {})

            # --- CASE 1: Reactive Solve (Complex Sub-goal) ---
            if tool_name == "reactive_solve":
                simple_sub_goal = parameters.get("sub_goal", "")
                logger.info(f"EXECUTOR (Step {step_id}): Generating TaskSpec for: '{simple_sub_goal}'")
                
                # 1. Generate TaskSpec (Architect Phase)
                task_spec = run_executor(
                    user_goal=goal['goal'], full_plan=goal.get('plan', []), 
                    strategy_blueprint=goal.get('strategy_blueprint', {}),
                    context_map=context_map, current_step_prompt=simple_sub_goal,
                    gemini_client=gemini_client, task_type="refine_subgoal"
                )
                
                # Ensure object type
                if isinstance(task_spec, dict):
                     try:
                        task_spec = ExecutorTaskSpec(**task_spec)
                     except Exception as e:
                        logger.error(f"Failed to cast TaskSpec: {e}")
                        task_spec = ExecutorTaskSpec(primary_tool="none", initial_inputs=[], task_description=simple_sub_goal)

                # 2. Run ReAct Loop with Hot Start
                result = _run_react_loop_with_hot_start(task_spec, context_map, goal)
                return step_id, result

            # --- CASE 2: Native Tool Direct Call ---
            elif tool_name in ["google_search", "get_maps_data", "execute_python_code"]:
                refined_prompt = run_executor(goal['goal'], [], {}, context_map, parameters.get("prompt") or parameters.get("query"), gemini_client, "refine_query")
                
                resp = execute_native_tool(tool_name, refined_prompt, active_tier)
                
                if resp == "RATE_LIMIT_HIT": return step_id, "RATE_LIMIT_HIT"
                return step_id, resp
                
            # --- CASE 3: Standard Tool ---
            else:
                return step_id, _execute_single_action(tool_call, context_map, goal)

        # --- CASE 4: Pure Prompt (No Tool) ---
        elif step.get("prompt"):
            refined_prompt = run_executor(goal['goal'], [], {}, context_map, step.get("prompt"), gemini_client, "refine_prompt")
            resp = gemini_client.ask_gemini(refined_prompt, tier=active_tier)
            
            if resp == "RATE_LIMIT_HIT": return step_id, "RATE_LIMIT_HIT"
            if isinstance(resp, str): return step_id, resp
            
            if resp and hasattr(resp, 'text') and resp.text: return step_id, resp.text
            return step_id, None

    except Exception as e:
        logger.error(f"Error executing step {step_id}: {e}", exc_info=True)
        return step_id, f"Error: {e}"

def _run_plan_monitor(user_goal: str, remaining_plan: list, last_step_output: str) -> str:
    """
    Intelligently checks if the last step's output actually moved the needle.
    If the output is garbage (e.g., 'I found nothing'), it triggers a REPLAN.
    """
    if not remaining_plan or len(str(last_step_output)) < 50:
        return "CONTINUE"
        
    logger.info("MONITOR: Checking step validity...")
    
    monitor_prompt = f"""
    You are a Quality Control AI. Check if the recent step output is useful and moves the plan forward.
    
    **USER GOAL:** "{user_goal}"
    **STEP OUTPUT:** "{str(last_step_output)[:1000]}..."
    
    **INSTRUCTIONS:**
    - If the output contains valid data (search results, code output, summaries), reply "CONTINUE".
    - If the output is a refusal ("I cannot do this"), an error ("No results found"), or hallucination, reply "REPLAN".
    
    Reply ONLY with "CONTINUE" or "REPLAN".
    """
    
    response = gemini_client.ask_gemini(monitor_prompt, tier='tier2', generation_config={"temperature": 0.0})
    
    if response and hasattr(response, 'text'):
        decision = response.text.strip().upper()
        if "REPLAN" in decision:
            logger.warning(f"MONITOR: Detected plan failure. Decision: {decision}")
            return "REPLAN"
            
    return "CONTINUE"

# =================================================================================================
# MAIN ORCHESTRATOR
# =================================================================================================

def main():
    """The main orchestrator loop."""
    logger.info("--- ⚙️ Orchestrator v12.0 Initializing (Refactored & Monitored) ---")
    last_active_time = time.time()
    
    while True:
        # 1. Efficient Waiting (Wake on Event or Timeout)
        orchestrator_wake_event.wait(timeout=30)
        if orchestrator_wake_event.is_set():
            logger.info(">>> WAKE SIGNAL RECEIVED! Resuming immediately.")
            orchestrator_wake_event.clear()

        # 2. Fetch Goal
        active_goal = get_active_goal()
        
        if active_goal:
            last_active_time = time.time()
            current_db_status = get_goal_status_by_id(active_goal['goal_id'])
            
            # Handle External Cancellation
            if current_db_status == 'cancelled':
                logger.warning(f"Orchestrator: Goal '{active_goal['goal_id']}' was cancelled externally. Dropping.")
                active_goal = None 
                continue 
            
            # Sync Status
            if current_db_status and current_db_status != active_goal['status']:
                 active_goal['status'] = current_db_status

            # --- STATUS: AWAITING REPLAN ---
            if active_goal.get('status') == 'awaiting_replan':
                logger.info(f"RE-PLANNER: Goal '{active_goal['goal_id']}' requires re-planning.")

                # Gather context from the failed run
                context_parts = []
                for step in active_goal.get('plan', []):
                    if step.get('status') == 'complete' and step.get('output'):
                        context_parts.append(f"Data from previous attempt (Step {step['step_id']}):\n{step['output']}\n---")
                existing_context_str = "\n".join(context_parts)

                archive_goal(active_goal['goal_id'])
                
                # Re-run Planner
                new_goal_obj = orchestrate_planning(
                    user_goal=active_goal['goal'],
                    preferred_tier=active_goal.get('preferred_tier', 'tier1'),
                    existing_context_str=existing_context_str
                )
                
                if new_goal_obj:
                    # Versioning for Re-plans
                    new_goal_obj['replan_count'] = active_goal.get('replan_count', 0) + 1
                    timestamp_str = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
                    new_goal_obj['goal_id'] = f"replan_{new_goal_obj['replan_count']}_{timestamp_str}"
                    
                    add_goal(new_goal_obj)
                    logger.info(f"RE-PLANNER: Created fresh plan: {new_goal_obj['goal_id']}")
                    status_update_queue.put("goal_updated")
                
                continue

            # --- STATUS: IN PROGRESS ---
            if active_goal.get('status') == 'pending' and active_goal.get('plan'):
                active_goal['status'] = 'in-progress'
            
            # Determine Executable Steps (Dependency Graph)
            completed_step_ids = {s['step_id'] for s in active_goal['plan'] if s['status'] == 'complete'}
            executable_steps = [s for s in active_goal['plan'] if s['status'] == 'pending' and set(s.get('dependencies', [])).issubset(completed_step_ids)]
            
            if not executable_steps:
               pass
            else:
                # 3a. Priority Execution: Heavyweight Tasks (e.g., reactive_solve)
                # We execute these one-by-one to allow for deep reasoning and monitoring.
                heavyweight_step = next(
                    (step for step in executable_steps 
                     if step and (step.get("tool_call") or {}).get("tool_name") == "reactive_solve"), 
                    None
                )
                
                if heavyweight_step:
                    logger.info(f"Prioritizing heavyweight task: Step {heavyweight_step.get('step_id')}.")

                    # --- CONTEXT CURATION (HYDRAULIC SYSTEM) ---
                    # Select only relevant context for this specific task
                    completed_steps_list = [s for s in active_goal['plan'] if s['status'] == 'complete']
                    current_task_desc = heavyweight_step.get('prompt') or str(heavyweight_step.get('tool_call'))
                    context_map = ContextCurator.get_relevant_context(current_task_desc, completed_steps_list)
                    # -------------------------------------------

                    step_id, response = _execute_step(heavyweight_step, active_goal, context_map)
                    
                    if response:
                        heavyweight_step['output'] = response
                        # Generate summary for the Curator Index
                        heavyweight_step['summary'] = _generate_step_summary(response)
                        heavyweight_step['status'] = 'complete'
                        
                        # --- MONITOR CHECK ---
                        # If the result is bad, trigger a replan immediately
                        remaining = [s for s in active_goal['plan'] if s['status'] == 'pending' and s['step_id'] > step_id]
                        if _run_plan_monitor(active_goal['goal'], remaining, response) == "REPLAN":
                             active_goal['status'] = 'awaiting_replan'
                             update_goal(active_goal)
                             status_update_queue.put("goal_updated")
                             continue 

                        update_goal(active_goal)
                        status_update_queue.put("goal_updated")

                # 3b. Swarm Execution: Parallel Tasks
                # Lighter tasks (simple tools, files) can run in parallel.
                else:
                    logger.info(f"Found a swarm of {len(executable_steps)} executable steps. Dispatching...")

                    completed_steps_list = [s for s in active_goal['plan'] if s['status'] == 'complete']

                    with concurrent.futures.ThreadPoolExecutor(max_workers=len(executable_steps)) as executor:
                        future_to_step = {}

                        for step in executable_steps:
                            # --- PER-THREAD CONTEXT CURATION ---
                            current_task_desc = step.get('prompt') or str(step.get('tool_call'))
                            step_context = ContextCurator.get_relevant_context(current_task_desc, completed_steps_list)

                            future = executor.submit(_execute_step, step, active_goal, step_context)
                            future_to_step[future] = step
                        
                        for future in concurrent.futures.as_completed(future_to_step):
                            step = future_to_step[future]
                            step_id, response = future.result()
                            
                            if response and response not in ["AWAITING_USER_INPUT_SIGNAL", "RATE_LIMIT_HIT"]:
                                step['output'] = response
                                step['summary'] = _generate_step_summary(response)
                                step['status'] = 'complete'
                                logger.info(f"Step {step_id} completed successfully in swarm.")
                                
                                # Monitor check for critical failures in swarm
                                remaining = [s for s in active_goal['plan'] if s['status'] == 'pending' and s['step_id'] > step_id]
                                if _run_plan_monitor(active_goal['goal'], remaining, response) == "REPLAN":
                                    active_goal['status'] = 'awaiting_replan'
                                    break
                            elif response == "AWAITING_USER_INPUT_SIGNAL":
                                active_goal['status'] = 'awaiting_input'
                            elif response == "RATE_LIMIT_HIT":
                                logger.warning(f"Step {step_id} hit rate limit.")
                            elif response is None:
                                retries = step.get('retries', 0)
                                if retries < MAX_RETRIES:
                                    step['retries'] = retries + 1
                                    logger.warning(f"Step {step_id} failed. Retrying...")
                                else:
                                    active_goal['status'] = 'paused'
                                    update_goal(active_goal)
                                    status_update_queue.put("goal_updated")
                            else:
                                step['status'] = 'failed'
                                active_goal['status'] = 'failed'
                                update_goal(active_goal)
                                archive_goal(active_goal['goal_id'])
                                status_update_queue.put("goal_updated")
                                break
                            
                if active_goal['status'] != 'failed':
                    update_goal(active_goal)
                    status_update_queue.put("goal_updated")

        # 4. Background Tasks (Summary, DMN, Sleep)
        elif should_trigger_summary():
            generate_eod_summary(memory_manager, gemini_client)
        elif should_trigger_dmn(rate_limiter, last_active_time):
             run_dmn_tasks(gemini_client, memory_manager)
        else:
            logger.info("-> No active goals. Deep Sleep.")
            orchestrator_wake_event.wait(timeout=30)
            if orchestrator_wake_event.is_set(): orchestrator_wake_event.clear()
