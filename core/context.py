import queue
import logging # NEW: Import the logging library

# First, import the now-independent logger.
from utils.logger import logger

# --- NEW: Define the custom handler here ---
class QueueHandler(logging.Handler):
    """A custom logging handler that puts the formatted log message on the status queue."""
    def __init__(self, queue):
        super().__init__()
        self.queue = queue

    def emit(self, record):
        log_entry = self.format(record)
        self.queue.put(log_entry)

# --- Now, create the shared components in the correct order ---
logger.info("Initializing shared context...")

# 1. Create the queue.
status_update_queue = queue.Queue()

# 2. Add the QueueHandler to the already-existing logger.
# This gives the logger its live-update capability without it needing to know why.
queue_handler = QueueHandler(status_update_queue)
# We need to set a formatter for this new handler as well
queue_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(module)s - %(message)s'))
logger.addHandler(queue_handler)
logger.info("Live dashboard logging handler attached.")

# 3. Import and create the other components that depend on the logger.
from utils.rate_limiter import RateLimitTracker
from utils.gemini_api import GeminiClient
from core.memory_manager import MemoryManager

rate_limiter = RateLimitTracker()
gemini_client = GeminiClient(rate_limiter)
memory_manager = MemoryManager()

logger.info("Shared context initialized successfully.")