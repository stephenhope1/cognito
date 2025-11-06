from flask import Flask, render_template, request, redirect, url_for, jsonify
from flask_socketio import SocketIO
import os
import uuid
import math
import threading          
from core.dmn import creative_synthesis_loop  
from core.context import logger, status_update_queue, gemini_client, memory_manager  # <-- ADD 'gemini_client' and 'memory_manager'
from datetime import datetime
from utils.database import (
    add_goal as db_add_goal, 
    get_active_goals, 
    get_archived_goals, 
    get_goal_by_id, 
    update_goal, 
    archive_goal,
    update_goal_status,
    get_archived_goal_count,
    update_goal_tier
)
from core.context import logger, status_update_queue
from core.planner import orchestrate_planning

app = Flask(__name__)
socketio = SocketIO(app, async_mode='threading')
ARCHIVE_PER_PAGE = 10

def get_full_status_data():
    """Helper to gather all data for a dashboard update."""
    active = get_active_goals()
    archived_preview = get_archived_goals(page=1, per_page=10)
    
    log_content = "Log file not found."
    try:
        with open('logs/agent.log', 'r', encoding='utf-8') as f:
            log_lines = f.readlines()[-20:]
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
        'active_goals': active, 'archived_goals': archived_preview,
        'log_tail': log_content, 'summary_content': summary_content
    }

def _create_and_add_goal(goal_text: str, source: str):
    """
    Orchestrates the full Strategist -> Planner pipeline for a given text goal.
    This is the single source of truth for creating new goals.
    """
    logger.info(f"PLAN_ORCHESTRATOR: Starting new planning cycle for goal: '{goal_text}'")
    
    new_goal_obj = orchestrate_planning(goal_text)
    
    if new_goal_obj:

        timestamp_str = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        new_goal_obj['goal_id'] = f"{source}_{'clarification' if new_goal_obj.get('status') == 'awaiting_input' else 'goal'}_{timestamp_str}"
        
        db_add_goal(new_goal_obj)
        logger.info(f"Successfully created and added new goal '{new_goal_obj['goal_id']}' to database.")
    else:
        logger.error(f"Failed to create a plan for the goal: '{goal_text}'")
        
# --- Flask Routes ---
@app.route('/')
def home(): return render_template('index.html')

@app.route('/add_goal', methods=['POST'])
def add_goal():
    """Handles goal submission from the main input form."""
    goal_text = request.form.get('goal_text')
    if goal_text:
        _create_and_add_goal(goal_text, source="web")
    return redirect(url_for('home'))

@app.route('/api/goal/<goal_id>/set_tier', methods=['POST'])
def set_goal_tier(goal_id):
    """
    API endpoint to downgrade a goal's tier and set it back to pending.
    """
    new_tier = request.json.get('tier', 'tier2')
    
    try:
        # 1. Update the goal's preferred tier in the database
        update_goal_tier(goal_id, new_tier)
        
        # 2. Set the goal's status back to 'pending' so the orchestrator picks it up
        update_goal_status(goal_id, 'pending')
        
        return jsonify(success=True)
    except Exception as e:
        logger.error(f"Error setting tier for goal {goal_id}: {e}")
        return jsonify(error=str(e)), 500

@app.route('/provide_input', methods=['POST'])
def provide_input():
    """Handles submission from the user input form for a specific goal."""
    goal_id = request.form.get('goal_id')
    user_response = request.form.get('user_response')
    
    original_goal = get_goal_by_id(goal_id)
    
    if original_goal and user_response:
        # First, mark the old goal as complete
        original_goal['status'] = 'complete'
        update_goal(original_goal)
        # Then, archive it
        archive_goal(goal_id)
        
        # Create a new, refined goal to send back through the full pipeline
        refined_goal_text = f"Original Goal: '{original_goal['goal']}'. User Clarification: '{user_response}'"
        
        # Calls the *same* helper function
        _create_and_add_goal(refined_goal_text, source="clarification")
    else:
        logger.error(f"Could not find goal '{goal_id}' or user response was empty.")
        
    return redirect(url_for('home'))

@app.route('/api/goal/<goal_id>/set_status', methods=['POST'])
def set_goal_status(goal_id):
    """API endpoint to update the status of a goal."""
    status = request.json.get('status')
    if not status:
        return jsonify(error="Status not provided"), 400
    
    if status == 'cancelled':
        archive_goal(goal_id)
        update_goal_status(goal_id, 'cancelled') 
    else:
        update_goal_status(goal_id, status)
    
    return jsonify(success=True)

@app.route('/api/trigger_dmn', methods=['POST'])
def trigger_dmn():
    """
    API endpoint to manually trigger the DMN's creative_synthesis_loop
    in a background thread.
    """
    logger.info("DASHBOARD: Manual DMN trigger received.")
    
    def dmn_task():
        logger.info("DMN_THREAD: Starting creative synthesis loop...")
        with app.app_context():
            creative_synthesis_loop(gemini_client, memory_manager)
            # After the DMN runs and (potentially) adds a goal,
            # we ring the bell to force the dashboard to update.
            status_update_queue.put("goal_updated")
            logger.info("DMN_THREAD: Creative synthesis loop finished.")

    # Run the DMN in a background thread so the API request
    # returns immediately and doesn't time out.
    threading.Thread(target=dmn_task, daemon=True).start()
    
    return jsonify(success=True, message="DMN brainstorming started in background.")

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
    logger.info("Dashboard client connected via WebSocket.")
    socketio.emit('status_update', get_full_status_data())

def watch_status_queue():
    """A background thread that listens for updates and emits the correct event."""
    logger.info("Background thread started to watch for status updates.")
    while True:
        message = status_update_queue.get()
        with app.app_context():
            if message == "goal_updated":
                logger.info("Received 'goal_updated' signal. Pushing full status update.")
                socketio.emit('status_update', get_full_status_data())
            else: 
                socketio.emit('new_log_line', {'data': message})