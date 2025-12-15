import queue
import logging
import threading

# First, import the independent logger
from utils.logger import logger

# --- Queue Handler for Dashboard Streaming ---
class QueueHandler(logging.Handler):
    """A custom logging handler that puts the formatted log message on the status queue."""
    def __init__(self, queue):
        super().__init__()
        self.queue = queue

    def emit(self, record):
        log_entry = self.format(record)
        self.queue.put(log_entry)

logger.info("Initializing shared context...")

# 1. Create the Status Queue (Threading)
status_update_queue = queue.Queue()

# 2. Attach Handler
queue_handler = QueueHandler(status_update_queue)
queue_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(module)s - %(message)s'))
logger.addHandler(queue_handler)
logger.info("Live dashboard logging handler attached.")

# 3. NEW: Global Wake Event
# This event replaces time.sleep(). It allows the Orchestrator to sleep efficiently
# but wake up INSTANTLY when a new goal is added from anywhere in the system.
orchestrator_wake_event = threading.Event()

# 4. Initialize Components
from utils.rate_limiter import RateLimitTracker
from utils.gemini_api import GeminiClient
from core.memory_manager import MemoryManager

rate_limiter = RateLimitTracker()
gemini_client = GeminiClient(rate_limiter)
memory_manager = MemoryManager()

logger.info("Shared context initialized successfully.")