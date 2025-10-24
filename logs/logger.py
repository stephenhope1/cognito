import logging
import os

def setup_logger():
    """Sets up a centralized logger for the agent."""
    logs_dir = 'logs'
    os.makedirs(logs_dir, exist_ok=True)
    
    # Configure logging to file
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(module)s - %(message)s',
        handlers=[
            logging.FileHandler(os.path.join(logs_dir, "agent.log"), encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    # MODIFIED: Also set the encoding for the console handler
    logging.getLogger().handlers[1].setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(module)s - %(message)s'))
    logging.getLogger().handlers[1].stream.reconfigure(encoding='utf-8')

    return logging.getLogger(__name__)

logger = setup_logger()