# core/tools.py
import os
from utils.logger import logger
from utils.email_client import create_draft as draft_email_tool # Import the existing function
from utils.database import update_user_profile as update_user_profile_db

# --- NEW: Define the project's base directory for sandboxing ---
# os.path.abspath(__file__) -> /.../cognito/core/tools.py
# os.path.dirname(...) -> /.../cognito/core
# os.path.dirname(...) -> /.../cognito
PROJECT_BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
# --- END NEW ---


# --- Tool Manifest ---
# This is the "menu" of tools we will show to the Planner AI.
TOOL_MANIFEST = [
    # --- MODIFIED: Added back native tools ---
    {
        "tool_name": "google_search",
        "description": "A native tool for Google Search. Use this for real-time web research.",
        "parameters": [
            {"name": "prompt", "type": "string", "description": "The natural language query for Google Search (e.g., 'What is the capital of Canada?')."}
        ]
    },
    {
        "tool_name": "execute_python_code",
        "description": "A native, sandboxed Python code interpreter. Use this for math, data analysis, string manipulation, or any complex logic.",
        "parameters": [
            {"name": "prompt", "type": "string", "description": "A natural language prompt describing the problem to solve with code."}
        ]
    },
    {
        "tool_name": "get_maps_data",
        "description": "A native tool that connects to Google Maps. Use this to find places, get details about locations, or plan routes.",
        "parameters": [
            {"name": "prompt", "type": "string", "description": "A natural language prompt for the Google Maps query (e.g., 'Find coffee shops near the Eiffel Tower')."}
        ]
    },
    {
        "tool_name": "write_to_file",
        "description": "Writes given text content to a local file. Use this to save work, summaries, or final outputs.",
        "parameters": [
            {"name": "filename", "type": "string", "description": "The name of the file to be created, e.g., 'summary.txt' or 'draft.md'."},
            {"name": "content", "type": "string", "description": "The text content to write into the file."}
        ]
    },
    {
        "tool_name": "draft_email",
        "description": "Creates a draft email in the user's Gmail account.",
        "parameters": [
            {"name": "to", "type": "string", "description": "The recipient's email address."},
            {"name": "subject", "type": "string", "description": "The subject line of the email."},
            {"name": "body", "type": "string", "description": "The body content of the email."}
        ]
    },
    {
        "tool_name": "request_user_input",
        "description": "Pauses execution and asks the user for specific information or clarification. Use this when the goal is ambiguous or requires information you don't have.",
        "parameters": [
            {"name": "question", "type": "string", "description": "The specific question to ask the user."}
        ]
    },
    {
        "tool_name": "read_file",
        "description": "Reads the entire content of a specified text file from a safe directory and returns it as a string. Use this to get context from local files.",
        "parameters": [
            {"name": "filename", "type": "string", "description": "The name of the file to read from the 'data/inbox' or 'data/output' directory."}
    ]
    },
    {
        "tool_name": "read_internal_file",
        "description": "Reads one of the agent's own internal code files (like 'planner.py' or 'main.py') to understand its own logic. Cannot read data files or sensitive files.",
        "parameters": [
            {"name": "filename", "type": "string", "description": "The name of the internal file to read (e.g., 'core/planner.py')."}
    ]
    },
    {
        "tool_name": "reactive_solve",
        "description": "Engages a specialized, step-by-step reasoning loop to solve complex, dynamic, or unpredictable sub-goals. Use this for tasks like debugging code, in-depth analysis with unknown steps, or any problem that requires a 'think, then act' cycle.",
        "parameters": [
            {"name": "sub_goal", "type": "string", "description": "The specific, complex sub-goal that needs to be solved reactively."}
        ]
    },
    {
        "tool_name": "update_user_profile",
        "description": "Updates the user's persistent profile with a new key-value pair. Use this to save learned preferences, interests, or facts about the user.",
        "parameters": [
            {"name": "key", "type": "string", "description": "The profile key to save (e.g., 'primary_interest', 'preferred_format')."},
            {"name": "value", "type": "string", "description": "The value to save for that key."},
            {"name": "source", "type": "string", "description": "The source of this information (e.g., 'live_chat_inference', 'post-mortem_reflexion')."}
        ]
    }
]

# --- Tool Implementations ---
# These are the actual Python functions that get called by the Orchestrator.

def read_file_tool(filename: str) -> str:
    """A tool that reads the content of a file from a sandboxed directory."""
    # This tool is for user data (inbox/output)
    allowed_dirs = [os.path.abspath('data/inbox'), os.path.abspath('data/output')]
    
    try:
        for safe_dir in allowed_dirs:
            file_path = os.path.abspath(os.path.join(safe_dir, filename))
            
            if os.path.commonpath([safe_dir]) == os.path.commonpath([safe_dir, file_path]):
                if os.path.exists(file_path):
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    logger.info(f"Successfully read file: {file_path}")
                    return content
        
        logger.error(f"Error reading file: '{filename}' not found in any allowed directory.")
        return f"Error: File '{filename}' not found in allowed directories."
            
    except Exception as e:
        logger.error(f"Error reading file '{filename}': {e}")
        return f"Error: Could not read file."

def read_internal_file_tool(filename: str) -> str:
    """
    A tool that reads one of the agent's own code files,
    sandboxed to the project directory.
    """
    try:
        # --- CRITICAL SECURITY GUARDRAILS ---
        # 1. Normalize the path
        file_path = os.path.abspath(os.path.join(PROJECT_BASE_DIR, filename))

        # 2. Prevent directory traversal (e.g., '../')
        if not file_path.startswith(PROJECT_BASE_DIR):
            logger.warning(f"SECURITY: Denied attempt to read file outside project directory: {filename}")
            return "Error: Access denied. File is outside the allowed project directory."
            
        # 3. Block sensitive files and directories
        sensitive_paths = ['data/', 'logs/', '.env', '.git', 'node_modules']
        normalized_filename = filename.replace('\\', '/')
        if any(sensitive in normalized_filename for sensitive in sensitive_paths):
            logger.warning(f"SECURITY: Denied attempt to read sensitive file/directory: {filename}")
            return "Error: Access denied. Cannot read sensitive files or data directories."
        # --- END GUARDRAILS ---

        if os.path.exists(file_path) and os.path.isfile(file_path):
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            logger.info(f"Successfully read internal file: {file_path}")
            return content
        else:
            logger.error(f"Error reading internal file: '{filename}' not found.")
            return f"Error: File '{filename}' not found."
            
    except Exception as e:
        logger.error(f"Error reading internal file '{filename}': {e}")
        return f"Error: Could not read file."
    
def write_to_file_tool(filename: str, content: str) -> str:
    """A tool that writes content to a file in the 'data/output' directory."""
    try:
        output_dir = 'data/output'
        os.makedirs(output_dir, exist_ok=True)
        file_path = os.path.join(output_dir, filename)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        logger.info(f"Successfully wrote to file: {file_path}")
        return f"Success: Content saved to {file_path}"
    except Exception as e:
        logger.error(f"Error writing to file '{filename}': {e}")
        return f"Error: Could not write to file."

TOOL_EXECUTOR = {
    "write_to_file": write_to_file_tool,
    "draft_email": draft_email_tool,
    "read_file": read_file_tool,
    "read_internal_file": read_internal_file_tool,
    "update_user_profile": update_user_profile_db
    # Native tools (search, maps, code) are handled in main.py
}