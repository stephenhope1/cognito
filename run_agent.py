import threading
import multiprocessing
import time
import uuid
from logs.logger import logger

# Import the main functions/apps
from main import main as run_orchestrator
from dashboard import app as dashboard_app
from core.file_watcher import main as run_file_watcher
from voice_interface import main as run_voice_interface

# Import the necessary components for the new planning pipeline
from core.context import gemini_client
from core.strategist import run_strategist
from core.planner import generate_plan
from utils.database import add_goal

def run_dashboard():
    """Starts the Flask web server in a separate thread."""
    logger.info("Starting dashboard thread...")
    dashboard_app.run(host='0.0.0.0', port=5000, debug=False)

def voice_command_listener(queue):
    """A thread that listens for transcribed voice commands and orchestrates planning."""
    logger.info("Voice command listener thread started.")
    while True:
        try:
            # This will block until a command is received from the voice process
            goal_text = queue.get()
            
            logger.info(f"VOICE QUEUE: Received goal: '{goal_text}'")
            
            # --- NEW TWO-STAGE PLANNING LOGIC ---

            # 1. Run the Strategist to get the blueprint
            strategy_blueprint = run_strategist(goal_text)
            if not strategy_blueprint:
                logger.error("VOICE QUEUE: Strategist failed to generate a blueprint.")
                continue # Wait for the next command

            # 2. Check if the Strategist requires user clarification
            if strategy_blueprint.get("requires_clarification"):
                logger.info("VOICE QUEUE: Strategy requires user input. Pausing goal.")
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
                    "goal_id": f"voice_clarification_{uuid.uuid4()}", "goal": goal_text,
                    "plan": clarification_plan,
                    "audit_critique": "Awaiting user clarification before full planning.",
                    "status": "awaiting_input",
                    "strategy_blueprint": strategy_blueprint
                }
            else:
                # 3. If no clarification is needed, run the Planner
                final_plan = generate_plan(goal_text, strategy_blueprint, gemini_client)
                if not final_plan:
                    logger.error("VOICE QUEUE: Planner failed to generate a plan from the blueprint.")
                    continue # Wait for the next command

                new_goal = {
                    "goal_id": f"voice_goal_{uuid.uuid4()}", "goal": goal_text,
                    "plan": [{**step, "status": "pending", "output": None} for step in final_plan],
                    "audit_critique": f"Plan generated using '{strategy_blueprint.get('cognitive_gear')}' gear.",
                    "status": "pending",
                    "strategy_blueprint": strategy_blueprint
                }

            add_goal(new_goal)
            logger.info(f"VOICE QUEUE: Successfully created and added new goal '{new_goal['goal_id']}' to the database.")

        except Exception as e:
            logger.error(f"Error in voice command listener: {e}")

if __name__ == "__main__":
    from utils.database import initialize_database
    initialize_database()

    logger.info("--- LAUNCHING COGNITO AGENT ---")

    command_queue = multiprocessing.Queue()
    
    services = {
        "Orchestrator": {"target": run_orchestrator, "type": "thread"},
        "File_Watcher": {"target": run_file_watcher, "type": "thread"},
        "Dashboard": {"target": run_dashboard, "type": "thread"},
        "Voice_Command_Listener": {"target": voice_command_listener, "args": (command_queue,), "type": "thread"},
        "Voice_Interface": {"target": run_voice_interface, "args": (command_queue,), "type": "process"}
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