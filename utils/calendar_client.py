import os.path
import datetime as dt
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Defines what our app is allowed to do: read-only access to calendar events
SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']
TOKEN_PATH = 'token.json'
CREDENTIALS_PATH = 'credentials.json'

def get_upcoming_events(hours=24):
    """Shows basic usage of the Google Calendar API."""
    creds = None
    # The file token.json stores the user's access and refresh tokens.
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        # Save the credentials for the next run
        with open(TOKEN_PATH, 'w') as token:
            token.write(creds.to_json())

    try:
        service = build('calendar', 'v3', credentials=creds)
        now = dt.datetime.utcnow().isoformat() + 'Z'  # 'Z' indicates UTC time
        future_time = (dt.datetime.utcnow() + dt.timedelta(hours=hours)).isoformat() + 'Z'

        events_result = service.events().list(
            calendarId='primary', timeMin=now, timeMax=future_time,
            maxResults=10, singleEvents=True, orderBy='startTime'
        ).execute()
        events = events_result.get('items', [])

        if not events:
            return None

        # Format events into a simple string
        event_list = []
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            event_list.append(f"- {event['summary']} (at {start})")
        return "\n".join(event_list)

    except Exception as e:
        print(f"An error occurred with the Calendar API: {e}")
        return None

if __name__ == '__main__':
    print("--- Testing Google Calendar Client ---")
    upcoming_events = get_upcoming_events()
    if upcoming_events:
        print("\n--- Upcoming Events ---")
        print(upcoming_events)
    else:
        print("No upcoming events found or an error occurred.")