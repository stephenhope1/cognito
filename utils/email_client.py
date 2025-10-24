import os.path
import base64
from email.mime.text import MIMEText
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from logs.logger import logger

# This new scope allows creating drafts. It's more permissive than the calendar's.
SCOPES = ['https://www.googleapis.com/auth/gmail.modify']
TOKEN_PATH = 'token.json'
CREDENTIALS_PATH = 'credentials.json'

def get_gmail_service():
    """Authenticates with Google and returns a Gmail service object."""
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, 'w') as token:
            token.write(creds.to_json())
    return build('gmail', 'v1', credentials=creds)

def create_draft(to: str, subject: str, body: str) -> str:
    """Creates a draft email in the user's Gmail account."""
    try:
        service = get_gmail_service()
        message = MIMEText(body)
        message['to'] = to
        message['subject'] = subject
        
        # The Gmail API requires the message to be base64url encoded
        create_message = {'message': {'raw': base64.urlsafe_b64encode(message.as_bytes()).decode()}}
        
        draft = service.users().drafts().create(userId='me', body=create_message).execute()
        logger.info(f"Successfully created draft. Draft ID: {draft['id']}")
        return f"Draft email created successfully for {to} with subject '{subject}'."
    except Exception as e:
        logger.error(f"An error occurred while creating email draft: {e}")
        return "Error: Failed to create the email draft."

if __name__ == '__main__':
    logger.info("--- Testing Gmail Client: Creating a sample draft ---")
    create_draft("test@example.com", "Cognito Agent Test", "This is a test draft from the Cognito AI agent.")