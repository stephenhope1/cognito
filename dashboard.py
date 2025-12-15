from flask import Flask, render_template, request, redirect, url_for, jsonify
from flask_socketio import SocketIO
import json
import os
import uuid
import math
import threading
import logging # Added for log level configuration
from datetime import datetime

# --- Imports from your original file ---
from core.dmn import creative_synthesis_loop  
from core.context import logger, status_update_queue, gemini_client, memory_manager, rate_limiter # Added rate_limiter
from utils.database import (
    add_goal as db_add_goal, 
    get_active_goals, 
    get_archived_goals, 
    get_goal_by_id, 
    update_goal, 
    archive_goal,
    update_goal_status,
    get_archived_goal_count,
    update_goal_tier,
    get_user_profile,
    update_user_profile
)
# Use the new orchestrator_wake_event from context
from core.context import orchestrator_wake_event
from core.planner import orchestrate_planning
from utils.goal_manager import create_and_add_goal
from core.agent_profile import get_agent_profile
from core.tools import TOOL_MANIFEST
from google.genai import types

# --- NOISE REDUCTION (Requested Change) ---
# Disable the default Werkzeug logger to unclog the terminal
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR) 

app = Flask(__name__)
# Disable SocketIO logs
socketio = SocketIO(app, async_mode='threading', logger=False, engineio_logger=False)
ARCHIVE_PER_PAGE = 10

def get_full_status_data():
    """Helper to gather all data for a dashboard update."""
    active = get_active_goals()
    archived_preview = get_archived_goals(page=1, per_page=10)
    
    log_content = "Log file not found."
    try:
        with open('logs/agent.log', 'r', encoding='utf-8') as f:
            # Read last 50 lines for better context
            log_lines = f.readlines()[-50:]
            log_content = "".join(reversed(log_lines))
    except Exception: pass

    summary_content = "Today's summary has not been generated yet."
    try:
        today_str = datetime.now().strftime('%Y-%m-%d')
        summary_filename = f"data/reports/{today_str}_summary.md"
        if os.path.exists(summary_filename):
            with open(summary_filename, 'r', encoding='utf-8') as f:
                summary_content = f.read()
    except Exception: pass

    return {
        'active_goals': active, 
        'archived_goals': archived_preview,
        'log_tail': log_content, 
        'summary_content': summary_content
    }

# --- FLASK ROUTES ---

@app.route('/')
def home(): 
    return render_template('index.html')

@app.route('/add_goal', methods=['POST'])
def add_goal():
    """Handles goal submission from the main input form."""
    goal_text = request.form.get('goal_text')
    if goal_text:
        create_and_add_goal(goal_text, source="web")
    return redirect(url_for('home'))

# --- NEW: Tier Management Route ---
@app.route('/api/goal/<goal_id>/set_tier', methods=['POST'])
def set_goal_tier_route(goal_id):
    """API endpoint to change a goal's tier."""
    new_tier = request.json.get('tier', 'tier2')
    try:
        update_goal_tier(goal_id, new_tier)
        # If it was paused due to rate limits, resume it
        # But check current status first to avoid reviving cancelled goals
        current_goal = get_goal_by_id(goal_id)
        if current_goal and current_goal['status'] in ['paused', 'awaiting_tier_decision']:
            update_goal_status(goal_id, 'pending')
        
        # Wake up orchestrator to process the change
        orchestrator_wake_event.set()
        return jsonify(success=True)
    except Exception as e:
        logger.error(f"Error setting tier for goal {goal_id}: {e}")
        return jsonify(error=str(e)), 500

# --- NEW: Status Management Route (Zombie Killer) ---
@app.route('/api/goal/<goal_id>/set_status', methods=['POST'])
def set_goal_status_route(goal_id):
    """API endpoint to update the status of a goal."""
    status = request.json.get('status')
    if not status:
        return jsonify(error="Status not provided"), 400
    
    if status == 'cancelled':
        archive_goal(goal_id)
        # We update status AFTER archiving to ensure the orchestrator loop sees it
        # update_goal_status handles the DB logic
        update_goal_status(goal_id, 'cancelled') 
    else:
        update_goal_status(goal_id, status)
    
    # Wake up orchestrator so it notices the cancellation immediately
    orchestrator_wake_event.set()
    return jsonify(success=True)

# --- NEW: Manual DMN Trigger ---
@app.route('/api/trigger_dmn', methods=['POST'])
def trigger_dmn():
    """API endpoint to manually trigger the DMN."""
    logger.info("DASHBOARD: Manual DMN trigger received.")
    
    def dmn_task():
        with app.app_context():
            creative_synthesis_loop(gemini_client, memory_manager)
            status_update_queue.put("goal_updated")
            orchestrator_wake_event.set()

    threading.Thread(target=dmn_task, daemon=True).start()
    return jsonify(success=True, message="DMN brainstorming started in background.")

# --- NEW: Rate Limit API ---
@app.route('/api/rate_limits', methods=['GET'])
def get_rate_limits():
    """Returns current usage stats for the dashboard."""
    try:
        t1_usage = rate_limiter.get_daily_usage_percentage('tier1')
        t2_usage = rate_limiter.get_daily_usage_percentage('tier2')
        
        return jsonify({
            "tier1": {"usage_pct": t1_usage, "limit": 50},
            "tier2": {"usage_pct": t2_usage, "limit": 250}
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/provide_input', methods=['POST'])
def provide_input():
    """Handles submission from the user input form for a specific goal."""
    goal_id = request.form.get('goal_id')
    user_response = request.form.get('user_response')
    
    original_goal = get_goal_by_id(goal_id)
    
    if original_goal and user_response:
        original_goal['status'] = 'complete'
        update_goal(original_goal)
        archive_goal(goal_id)
        
        refined_goal_text = f"Original Goal: '{original_goal['goal']}'. User Clarification: '{user_response}'"
        create_and_add_goal(refined_goal_text, source="clarification") 
    else:
        logger.error(f"Could not find goal '{goal_id}' or user response was empty.")
        
    return redirect(url_for('home'))

@app.route('/archive')
def view_archive():
    page = request.args.get('page', 1, type=int)
    total_goals = get_archived_goal_count()
    total_pages = math.ceil(total_goals / ARCHIVE_PER_PAGE)
    archived_goals = get_archived_goals(page=page, per_page=ARCHIVE_PER_PAGE)
    return render_template('archive.html', goals=archived_goals, page=page, total_pages=total_pages)

@app.route('/logs')
def full_log_viewer():
    log_content = "Log file not found."
    search_query = request.args.get('q', '')
    try:
        with open('logs/agent.log', 'r', encoding='utf-8') as f:
            log_lines = f.readlines()
            if search_query:
                log_lines = [line for line in log_lines if search_query.lower() in line.lower()]
            log_content = "".join(log_lines).replace('\n', '<br>')
    except Exception: pass
    return render_template('logs.html', log_content=log_content, search_query=search_query)

@app.route('/summaries')
def summary_archive():
    report_dir = 'data/reports'
    summaries = []
    if os.path.exists(report_dir):
        summaries = sorted([f for f in os.listdir(report_dir) if f.endswith('_summary.md')], reverse=True)
    return render_template('summaries.html', summaries=summaries)

@app.route('/summaries/<filename>')
def view_single_summary(filename):
    summary_content = "Summary file not found."
    try:
        if '..' in filename or filename.startswith('/'): raise ValueError("Invalid filename")
        file_path = os.path.join('data/reports', filename)
        with open(file_path, 'r', encoding='utf-8') as f:
            summary_content = f.read().replace('\n', '<br>')
    except Exception as e: summary_content = f"Error: {e}"
    return render_template('summary_single.html', summary_content=summary_content)

# --- WebSocket Events ---

@socketio.on('connect')
def handle_connect():
    # Removed logger.info call to reduce noise
    socketio.emit('status_update', get_full_status_data())

def watch_status_queue():
    """A background thread that listens for updates and emits the correct event."""
    logger.info("Background thread started to watch for status updates.")
    while True:
        message = status_update_queue.get()
        with app.app_context():
            if message == "goal_updated":
                socketio.emit('status_update', get_full_status_data())
            else: 
                socketio.emit('new_log_line', {'data': message})

def get_chat_context():
    """Gathers all context for the chat agent."""
    profile = get_user_profile()
    profile_str = json.dumps(profile, indent=2) if profile else "No profile data exists yet."
    recent_tasks = get_archived_goals(page=1, per_page=3)
    tasks_str = "\n".join([f"- {g['goal']} (Status: {g['status']})" for g in recent_tasks]) if recent_tasks else "No recent tasks."
    agent_profile = get_agent_profile(for_planner=False)
    return profile_str, tasks_str, agent_profile

def get_chat_tools() -> list[types.Tool]:
    """Builds the list of tools the chat agent can use."""
    chat_tool_list = []
    for tool_def in TOOL_MANIFEST:
        if tool_def['tool_name'] == 'update_user_profile':
            func = types.FunctionDeclaration(
                name=tool_def['tool_name'],
                description=tool_def['description'],
            )
            schema_properties = {}
            required_params = []
            for param in tool_def.get('parameters', []):
                param_name = param['name']
                type_mapping = {"string": "STRING"}
                schema_properties[param_name] = types.Schema(
                    type=type_mapping.get(param['type'], "STRING"),
                    description=param.get('description')
                )
                required_params.append(param_name)
            func.parameters = types.Schema(
                type="OBJECT",
                properties=schema_properties,
                required=required_params
            )
            chat_tool_list.append(types.Tool(function_declarations=[func]))
    return chat_tool_list

@app.route('/api/chat', methods=['POST'])
def chat():
    """
    Handles a single turn of a text-based chat conversation.
    """
    data = request.json
    user_message = data.get('message')
    chat_history = data.get('history', [])

    if not user_message: return jsonify(error="No message provided"), 400

    try:
        profile_str, tasks_str, agent_profile = get_chat_context()
        
        system_instruction = f"""
        You are Cognito, a proactive AI partner. Your primary goal right now is to have a natural, friendly conversation.
        **--- CONTEXT: YOUR AGENT PROFILE ---** {agent_profile}
        **--- CONTEXT: USER PROFILE ---** {profile_str}
        **--- CONTEXT: RECENT TASKS ---** {tasks_str}
        """
        
        chat_tools = get_chat_tools()
        api_history = []
        for turn in chat_history:
            api_history.append(types.Content(role=turn['role'], parts=[types.Part(text=turn['message'])]))
        api_history.append(types.Content(role="user", parts=[types.Part(text=user_message)]))
        
        response = gemini_client.ask_gemini(
            prompt=api_history, 
            tier='tier1', 
            generation_config={"temperature": 0.7}, 
            tools=chat_tools, 
            system_instruction=system_instruction
        )
        
        if not response: return jsonify(error="API call failed"), 500

        response_part = response.candidates[0].content.parts[0]
        
        if response_part.function_call:
            fc = response_part.function_call
            if fc.name == 'update_user_profile':
                update_user_profile(fc.args.get('key'), fc.args.get('value'), "live_chat")
                api_history.append(response.candidates[0].content)
                api_history.append(types.Content(role="function", parts=[types.Part(function_response=types.FunctionResponse(name="update_user_profile", response={"status": "success"}))]))
                
                response = gemini_client.ask_gemini(prompt=api_history, tier='tier1', generation_config={"temperature": 0.7}, tools=chat_tools, system_instruction=system_instruction)
                if not response: return jsonify(error="API call failed after tool use"), 500
                return jsonify(reply=response.text)
        
        if response.text:
            return jsonify(reply=response.text)
        else:
            return jsonify(reply="[No text response generated]")

    except Exception as e:
        logger.error(f"Error in /api/chat: {e}", exc_info=True)
        return jsonify(error=str(e)), 500