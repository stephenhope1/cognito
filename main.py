import time
import uuid
import os  # ADD THIS LINE
from datetime import datetime, timedelta
from core.context import rate_limiter, gemini_client, memory_manager, logger
from core.dmn import creative_synthesis_loop, generate_eod_summary
from core.tools import TOOL_EXECUTOR
from core.planner import generate_plan
from utils.database import get_active_goal, update_goal, archive_goal, add_goal

# --- Configuration ---
MAX_RETRIES = 2
IDLE_THRESHOLD_SECONDS = 300 # 5 minutes

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
    """
    Triggers a summary if the most recent one is more than 24 hours old.
    """
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
        logger.info(f"Daily briefing trigger: Last summary was over 24 hours ago ({last_mod_time.strftime('%Y-%m-%d %H:%M')}). Generating a new one.")
        return True

    return False

def main():
    """The main orchestrator loop for the Cognito agent."""
    logger.info("--- ⚙️ Orchestrator v3.4 Initializing (Rolling Summary) ---")
    last_active_time = time.time()
    
    while True:
        logger.info("\n--- Orchestrator waking up... ---")
        
        active_goal = get_active_goal()
        
        if active_goal:
            last_active_time = time.time()
            logger.info(f"-> Working on goal: '{active_goal['goal_id']}'")
            if active_goal['status'] == 'pending':
                active_goal['status'] = 'in-progress'
            
            next_step = next((s for s in active_goal['plan'] if s['status'] == 'pending'), None)
            
            if not next_step:
                final_status = 'complete' if all(s['status'] == 'complete' for s in active_goal['plan']) else 'failed'
                active_goal['status'] = final_status
                logger.info(f"All steps of goal '{active_goal['goal_id']}' are processed. Final status: {final_status}. Archiving.")
                update_goal(active_goal)
                archive_goal(active_goal['goal_id'])
            else:
                response = None
                context_map = {f"[output_of_step_{s.get('step_id')}]": s.get('output', '') for s in active_goal['plan'] if s['status'] == 'complete'}
                if "tool_call" in next_step:
                    tool_call = next_step["tool_call"]; tool_name = tool_call.get("tool_name"); parameters = tool_call.get("parameters", {})
                    logger.info(f"Executing Step {next_step.get('step_id', 'N/A')}: Using tool '{tool_name}'")
                    for key, value in parameters.items():
                        if isinstance(value, str) and value in context_map: parameters[key] = context_map[value]
                    if tool_name == "google_search":
                        response = gemini_client.ask_gemini(parameters.get("query", ""), tier='tier1', enable_search=True)
                    elif tool_name == "request_user_input":
                        active_goal['status'] = 'awaiting_input'; response = "Paused."
                    elif tool_name in TOOL_EXECUTOR:
                        response = TOOL_EXECUTOR[tool_name](**parameters)
                    else:
                        response = f"Error: Unknown tool '{tool_name}'"
                elif "prompt" in next_step:
                    prompt = next_step.get('prompt', ''); logger.info(f"Executing Step {next_step.get('step_id', 'N/A')}: {prompt}")
                    should_search = "google_search" in prompt.lower()
                    context_str = "\n".join([f"CONTEXT FROM PREVIOUS STEP ({k.strip('[]')}): {v}" for k, v in context_map.items()])
                    final_prompt = context_str + f"\n\n---\nBased on the context above, perform this task: {prompt}"
                    response = gemini_client.ask_gemini(final_prompt, tier='tier1', enable_search=should_search)
                if response:
                    next_step['output'] = response; next_step['status'] = 'complete'
                    logger.info(f"Step {next_step.get('step_id', 'N/A')} completed successfully.")
                    update_goal(active_goal)
                else:
                    retries = next_step.get('retries', 0)
                    if retries < MAX_RETRIES:
                        next_step['retries'] = retries + 1; logger.warning(f"Step {next_step.get('step_id', 'N/A')} failed. Retrying (attempt {retries + 1}/{MAX_RETRIES})...")
                        update_goal(active_goal)
                    else:
                        next_step['output'] = f"Step failed after {MAX_RETRIES + 1} attempts."; next_step['status'] = 'failed'; active_goal['status'] = 'failed'
                        logger.error(f"Step {next_step.get('step_id', 'N/A')} failed permanently. Halting and archiving goal.")
                        update_goal(active_goal); archive_goal(active_goal['goal_id'])

        elif should_trigger_summary():
            logger.info("\n>>> Daily Briefing Triggered: Last report is over 24 hours old. <<<")
            generate_eod_summary(memory_manager, gemini_client)

        elif should_trigger_dmn(rate_limiter, last_active_time):
            logger.info("\n>>> DMN TRIGGERED: Entering autonomous reflection mode. <<<")
            creative_synthesis_loop(gemini_client, memory_manager)
            last_active_time = time.time()
            
        else:
            logger.info("-> No active goals found. Standing by.")
            
        logger.info("--- Orchestrator going to sleep for 30 seconds. ---")
        time.sleep(30)