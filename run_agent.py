import threading
import multiprocessing
import time
import uuid
import datetime
from utils.logger import logger
from main import main as run_orchestrator
from dashboard import app as dashboard_app, socketio, watch_status_queue
from core.file_watcher import main as run_file_watcher
from voice_interface import main as run_voice_interface
from core.context import status_update_queue as main_process_queue # The threading queue

def run_dashboard():
    """Starts the Flask-SocketIO web server."""
    logger.info("Starting dashboard thread with WebSocket server...")
    socketio.run(dashboard_app, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)

def bridge_voice_logs(mp_queue, thread_queue):
    """
    Bridges the Multiprocessing Queue (Voice) to the Threading Queue (Dashboard).
    This allows logs from the separate Voice process to appear in the main dashboard.
    """
    while True:
        try:
            # Blocking get from the Voice process
            msg = mp_queue.get()
            if msg is None: break # Poison pill check
            # Put into the main process queue for the Dashboard to pick up
            thread_queue.put(msg)
        except Exception as e:
            logger.error(f"Error in log bridge: {e}")
            time.sleep(1)

if __name__ == "__main__":
    # 1. Initialize the robust, WAL-enabled database
    from utils.database import initialize_database
    initialize_database()

    logger.info("--- LAUNCHING COGNITO AGENT (Multi-Process) ---")
    
    # 2. Create the Multiprocessing Queue for the Voice Interface
    voice_status_queue = multiprocessing.Queue()

    # 3. Define Services
    services = {
        "Orchestrator": {"target": run_orchestrator, "type": "thread"},
        "File_Watcher": {"target": run_file_watcher, "type": "thread"},
        "Dashboard": {"target": run_dashboard, "type": "thread"},
        "Status_Queue_Watcher": {"target": watch_status_queue, "type": "thread"},
        # Bridge Thread
        "Log_Bridge": {"target": bridge_voice_logs, "args": (voice_status_queue, main_process_queue), "type": "thread"},
        # Voice Process (Pass the MP Queue)
        "Voice_Interface": {"target": run_voice_interface, "args": (voice_status_queue,), "type": "process"},
    }
    
    processes_and_threads = []
    
    for name, config in services.items():
        args = config.get("args", ())
        
        if config["type"] == "thread":
            instance = threading.Thread(target=config["target"], args=args, name=name, daemon=True)
        elif config["type"] == "process":
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
        # Optional: Send poison pills if needed, but daemon threads will die with main.

    logger.info("--- AGENT SHUTDOWN COMPLETE ---")