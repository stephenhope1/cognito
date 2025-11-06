import time
import uuid
import os
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from core.context import logger
from utils.database import add_goal
from core.planner import orchestrate_planning


# --- Configuration ---
WATCH_PATH = 'data/inbox'

class NewFileHandler(FileSystemEventHandler):
    """A handler for new file events that uses the full planning pipeline."""
    
    def on_created(self, event):
        if event.is_directory:
            return

        time.sleep(1) # Wait a moment to ensure the file is fully written
        
        file_path = event.src_path
        logger.info(f"📥 File detected: {file_path}")

        # MODIFIED: Create a natural language goal instead of a hard-coded plan.
        goal_text = f"A new file has been added to the inbox. Please read, analyze, and provide a concise summary of the document located at the absolute path: {os.path.abspath(file_path)}"

        # MODIFIED: Use the central planning orchestrator.
        new_goal_obj = orchestrate_planning(goal_text)

        if new_goal_obj:
            # Add the final unique ID and source before saving.
            new_goal_obj['goal_id'] = f"file_{'clarification' if new_goal_obj['status'] == 'awaiting_input' else 'goal'}_{uuid.uuid4()}"
            add_goal(new_goal_obj)
            logger.info(f"Successfully created and added new goal '{new_goal_obj['goal_id']}' for detected file.")
        else:
            logger.error(f"Failed to create a plan for the detected file: {file_path}")


def main():
    """The main loop for the file watcher thread."""
    logger.info(f"--- 👀 Cognito File Watcher Activated ---")
    logger.info(f"Watching for new files in: {os.path.abspath(WATCH_PATH)}")

    event_handler = NewFileHandler()
    observer = Observer()
    observer.schedule(event_handler, WATCH_PATH, recursive=False)
    
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
    logger.info("--- File Watcher Deactivated ---")