import logging
import os

# This file is now completely self-contained and has no external imports.

def setup_logger():
    """Sets up a centralized logger for the agent."""
    logs_dir = 'logs'
    os.makedirs(logs_dir, exist_ok=True)
    
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    
    # Prevent adding duplicate handlers if this function is called multiple times
    if logger.hasHandlers():
        logger.handlers.clear()

    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(module)s - %(message)s')

    # File Handler
    file_handler = logging.FileHandler(os.path.join(logs_dir, "agent.log"), encoding='utf-8')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Console Handler
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    try:
        stream_handler.stream.reconfigure(encoding='utf-8')
    except AttributeError:
        # Some environments might not have reconfigure, this is a safe fallback
        pass
    logger.addHandler(stream_handler)
    
    return logger

logger = setup_logger()