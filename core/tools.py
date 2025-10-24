# core/tools.py
import os
from logs.logger import logger
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
    }
]

# --- Tool Implementations ---
# These are the actual Python functions that get called by the Orchestrator.

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
    # "google_search" is special and handled directly in the Orchestrator
}