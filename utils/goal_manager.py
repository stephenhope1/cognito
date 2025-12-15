# new_file: utils/goal_manager.py
import datetime
from core.context import logger, orchestrator_wake_event # <-- Import the event
from core.planner import orchestrate_planning
from utils.database import add_goal

def create_and_add_goal(goal_text: str, source: str):
    """
    Orchestrates the full Strategist -> Planner pipeline for a given text goal
    and adds it to the database.
    """
    logger.info(f"PLAN_ORCHESTRATOR: Starting new planning cycle from '{source}' for goal: '{goal_text}'")
    
    new_goal_obj = orchestrate_planning(goal_text)
    
    if new_goal_obj:
        timestamp_str = datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        new_goal_obj['goal_id'] = f"{source}_{'clarification' if new_goal_obj.get('status') == 'awaiting_input' else 'goal'}_{timestamp_str}"
        
        add_goal(new_goal_obj)
        logger.info(f"Successfully created and added new goal '{new_goal_obj['goal_id']}' to database.")
        
        # --- CRITICAL: Wake up the Orchestrator! ---
        # If this is running in the same process (Dashboard), this works immediately.
        # If this is running in Voice Process, this sets a local event (no-op), 
        # but the LOG message above is caught by the bridge in run_agent.py.
        orchestrator_wake_event.set()
        
        return new_goal_obj
    else:
        logger.error(f"Failed to create a plan for the goal: '{goal_text}'")
        return None