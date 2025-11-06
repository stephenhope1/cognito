# core/tools.py
import os
from utils.logger import logger
from utils.email_client import create_draft as draft_email_tool # Import the existing function

# --- Tool Manifest ---
# This is the "menu" of tools we will show to the Planner AI.
TOOL_MANIFEST = [
    {
        "tool_name": "google_search",
        "description": "Performs a Google search to get real-time information or answer questions beyond your internal knowledge. Use this for any research-related task.",
        "parameters": [
            {"name": "query", "type": "string", "description": "The search query to be executed."}
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
        "tool_name": "reactive_solve",
        "description": "Engages a specialized, step-by-step reasoning loop to solve complex, dynamic, or unpredictable sub-goals. Use this for tasks like debugging code, in-depth analysis with unknown steps, or any problem that requires a 'think, then act' cycle.",
        "parameters": [
            {"name": "sub_goal", "type": "string", "description": "The specific, complex sub-goal that needs to be solved reactively."}
        ]
    },
    {
        "tool_name": "finish",
        "description": "Call this tool when the sub-goal is fully and completely achieved. This will exit the loop and return the final answer to the main plan.",
        "parameters": [
            {"name": "answer", "type": "string", "description": "A final one-sentence summary of the outcome."}
        ]
    }
]

# --- Tool Implementations ---
# These are the actual Python functions that get called by the Orchestrator.

def read_file_tool(filename: str) -> str:
    """A tool that reads the content of a file from a sandboxed directory."""
    # --- CRITICAL SECURITY GUARDRAIL ---
    # Define a list of safe directories the agent is allowed to read from.
    allowed_dirs = [os.path.abspath('data/inbox'), os.path.abspath('data/output')]
    
    try:
        # Try to build a path in each allowed directory
        for safe_dir in allowed_dirs:
            file_path = os.path.abspath(os.path.join(safe_dir, filename))
            
            # Check if the constructed path is genuinely within the safe directory
            if os.path.commonpath([safe_dir]) == os.path.commonpath([safe_dir, file_path]):
                if os.path.exists(file_path):
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    logger.info(f"Successfully read file: {file_path}")
                    return content
        
        # If the loop finishes without finding the file
        logger.error(f"Error reading file: '{filename}' not found in any allowed directory.")
        return f"Error: File '{filename}' not found in allowed directories."
            
    except Exception as e:
        logger.error(f"Error reading file '{filename}': {e}")
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

# This dictionary maps the tool names from the manifest to their actual Python functions.
TOOL_EXECUTOR = {
    "write_to_file": write_to_file_tool,
    "draft_email": draft_email_tool,
    "read_file": read_file_tool
    # "google_search" is special and handled directly in the Orchestrator
}