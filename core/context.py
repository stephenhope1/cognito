# core/context.py
from utils.rate_limiter import RateLimitTracker
from utils.gemini_api import GeminiClient
from core.memory_manager import MemoryManager
from logs.logger import logger

# This file creates single, shared instances of the agent's core components.
# All other modules will import these instances from here.

logger.info("Initializing shared context...")

# Create one RateLimiter to be shared by all processes.
rate_limiter = RateLimitTracker()

# Create one GeminiClient to be shared.
gemini_client = GeminiClient(rate_limiter)

# Create one MemoryManager to be shared.
memory_manager = MemoryManager()

logger.info("Shared context initialized successfully.")