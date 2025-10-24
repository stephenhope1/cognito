from flask import Flask, render_template, request, redirect, url_for
import os
import uuid
from datetime import datetime
from utils.database import add_goal as db_add_goal, get_active_goals, get_archived_goals, get_goal_by_id, update_goal, archive_goal
from core.context import gemini_client, logger
from core.strategist import run_strategist
from core.planner import generate_plan

app = Flask(__name__)

# --- Helper function for the main planning pipeline ---
def _create_goal_from_text(goal_text: str, source: str):
    """
    Orchestrates the full Strategist -> Planner pipeline for a given text goal.
    """
    logger.info(f"PLAN_ORCHESTRATOR: Starting new planning cycle for goal: '{goal_text}'")
    
    # 1. Run the Strategist to get the blueprint
    strategy_blueprint = run_strategist(goal_text)
    if not strategy_blueprint:
        logger.error("PLAN_ORCHESTRATOR: Strategist failed to generate a blueprint.")
        return

    # 2. Check if the Strategist requires user clarification
    if strategy_blueprint.get("requires_clarification"):
        logger.info("PLAN_ORCHESTRATOR: Strategy requires user input. Pausing goal.")
        clarification_plan = [{
            "step_id": 1,
            "dependencies": [],
            "tool_call": {
                "tool_name": "request_user_input",
                "parameters": {"question": strategy_blueprint.get("clarification_question")}
            },
            "status": "pending", "output": None
        }]
        new_goal = {
            "goal_id": f"{source}_clarification_{uuid.uuid4()}", "goal": goal_text,
            "plan": clarification_plan,
            "audit_critique": "Awaiting user clarification before full planning.",
            "status": "awaiting_input",
            "strategy_blueprint": strategy_blueprint # MODIFIED: Add blueprint
        }
    else:
        # 3. If no clarification is needed, run the Planner
        final_plan = generate_plan(goal_text, strategy_blueprint, gemini_client)
        if not final_plan:
            logger.error("PLAN_ORCHESTRATOR: Planner failed to generate a plan from the blueprint.")
            return

        new_goal = {
            "goal_id": f"{source}_goal_{uuid.uuid4()}", "goal": goal_text,
            "plan": [{**step, "status": "pending", "output": None} for step in final_plan],
            "audit_critique": f"Plan generated using '{strategy_blueprint.get('cognitive_gear')}' gear.",
            "status": "pending",
            "strategy_blueprint": strategy_blueprint # MODIFIED: Add blueprint
        }

    db_add_goal(new_goal)

# --- Flask Routes ---

@app.route('/')
def home():
    """Renders the main dashboard with active and archived goals."""
    active = get_active_goals()
    archived = get_archived_goals()
    return render_template('index.html', active_goals=active, archived_goals=archived)

@app.route('/log')
def view_log():
    """Displays the last 100 lines of the agent's log file."""
    try:
        with open('logs/agent.log', 'r', encoding='utf-8') as f:
            log_lines = f.readlines()[-100:]
        log_content = "<br>".join(log_lines)
    except FileNotFoundError:
        log_content = "Log file not found."
    return render_template('log.html', log_content=log_content)

@app.route('/summary')
def view_summary():
    """Displays today's end-of-day summary."""
    summary_content = "Today's summary has not been generated yet."
    try:
        today_str = datetime.now().strftime('%Y-%m-%d')
        summary_filename = f"data/reports/{today_str}_summary.md"
        if os.path.exists(summary_filename):
            with open(summary_filename, 'r', encoding='utf-8') as f:
                summary_content = f.read().replace('\n', '<br>')
    except Exception as e:
        summary_content = f"Error reading summary file: {e}"
    return render_template('summary.html', summary_content=summary_content)

@app.route('/add_goal', methods=['POST'])
def add_goal():
    """Handles goal submission from the main input form."""
    goal_text = request.form.get('goal_text')
    if goal_text:
        _create_goal_from_text(goal_text, source="web")
    return redirect(url_for('home'))

@app.route('/provide_input', methods=['POST'])
def provide_input():
    """Handles submission from the user input form for a specific goal."""
    goal_id = request.form.get('goal_id')
    user_response = request.form.get('user_response')
    
    original_goal = get_goal_by_id(goal_id)
    
    if original_goal and user_response:
        # Archive the old, ambiguous goal
        original_goal['status'] = 'complete'
        update_goal(original_goal)
        archive_goal(goal_id)
        
        # Create a new, refined goal to send back through the full pipeline
        refined_goal_text = f"Original Goal: '{original_goal['goal']}'. User has provided the following clarification: '{user_response}'"
        _create_goal_from_text(refined_goal_text, source="clarification")
    else:
        logger.error(f"Could not find goal '{goal_id}' or user response was empty.")
        
    return redirect(url_for('home'))