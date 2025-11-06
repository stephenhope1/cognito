import threading
import multiprocessing
import time
import uuid
from utils.logger import logger

# Import the main functions/apps
from main import main as run_orchestrator
from dashboard import app as dashboard_app, socketio, watch_status_queue # MODIFIED
from core.file_watcher import main as run_file_watcher
from voice_interface import main as run_voice_interface

# Import the necessary components for the planning pipeline
from core.planner import orchestrate_planning
from utils.database import add_goal

def run_dashboard():
    """Starts the Flask-SocketIO web server."""
    logger.info("Starting dashboard thread with WebSocket server...")
    # MODIFIED: Use socketio.run() to correctly start the server
    socketio.run(dashboard_app, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)

def _create_and_add_goal(goal_text: str, source: str):
    """
    Calls the planning orchestrator and adds the resulting goal to the database.
    """
    new_goal_obj = orchestrate_planning(goal_text)
    
    if new_goal_obj:
        timestamp_str = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        new_goal_obj['goal_id'] = f"cli_{'clarification' if new_goal_obj['status'] == 'awaiting_input' else 'goal'}_{timestamp_str}"
        add_goal(new_goal_obj)
        logger.info(f"Successfully created and added new goal '{new_goal_obj['goal_id']}' to database.")

    else:
        logger.error(f"Failed to create a plan for the goal: '{goal_text}'")

def voice_command_listener(queue):
    """A thread that listens for transcribed voice commands and orchestrates planning."""
    logger.info("Voice command listener thread started.")
    while True:
        try:
            goal_text = queue.get()
            logger.info(f"VOICE QUEUE: Received goal: '{goal_text}'")
            _create_and_add_goal(goal_text, source="voice")
        except Exception as e:
            logger.error(f"Error in voice command listener: {e}")

if __name__ == "__main__":
    from utils.database import initialize_database
    from dashboard import get_full_status_data # Import helper for _create_and_add_goal
    initialize_database()

    logger.info("--- LAUNCHING COGNITO AGENT ---")

    command_queue = multiprocessing.Queue()
    
    services = {
        "Orchestrator": {"target": run_orchestrator, "type": "thread"},
        "File_Watcher": {"target": run_file_watcher, "type": "thread"},
        "Dashboard": {"target": run_dashboard, "type": "thread"},
        "Voice_Command_Listener": {"target": voice_command_listener, "args": (command_queue,), "type": "thread"},
        "Voice_Interface": {"target": run_voice_interface, "args": (command_queue,), "type": "process"},
        "Status_Queue_Watcher": {"target": watch_status_queue, "type": "thread"} # MODIFIED: Add the new watcher
    }
    
    processes_and_threads = []
    
    for name, config in services.items():
        args = config.get("args", ())
        if config["type"] == "thread":
            instance = threading.Thread(target=config["target"], args=args, name=name, daemon=True)
        else:
            instance = multiprocessing.Process(target=config["target"], args=args, name=name, daemon=True)
        
        processes_and_threads.append(instance)
        instance.start()
        logger.info(f"Service '{name}' started as a {config['type']}.")

    logger.info("--- ALL SERVICES LAUNCHED ---")
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Ctrl+C received. Shutting down agent...")

    logger.info("--- AGENT SHUTDOWN COMPLETE ---")