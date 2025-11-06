import sqlite3
import json
from .logger import logger

DB_PATH = 'data/tasks.sqlite'

def initialize_database():
    """Creates the database with 'execution_log' and 'preferred_tier' columns."""
    try:
        con = sqlite3.connect(DB_PATH, check_same_thread=False)
        cur = con.cursor()
        # Add new TEXT columns 'execution_log' and 'preferred_tier'
        cur.execute('''
            CREATE TABLE IF NOT EXISTS goals (
                goal_id TEXT PRIMARY KEY, goal TEXT, plan TEXT, 
                audit_critique TEXT, status TEXT, strategy_blueprint TEXT,
                execution_log TEXT, preferred_tier TEXT 
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS archive (
                goal_id TEXT PRIMARY KEY, goal TEXT, plan TEXT,
                audit_critique TEXT, status TEXT, strategy_blueprint TEXT,
                execution_log TEXT, preferred_tier TEXT
            )
        ''')
        con.commit()
        con.close()
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")

def add_goal(goal_obj: dict):
    """Adds a new goal, ensuring execution_log and preferred_tier are present."""
    try:
        con = sqlite3.connect(DB_PATH, check_same_thread=False)
        cur = con.cursor()
        cur.execute("INSERT INTO goals VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
            goal_obj.get('goal_id'), goal_obj.get('goal'), json.dumps(goal_obj.get('plan')),
            goal_obj.get('audit_critique'), goal_obj.get('status'),
            json.dumps(goal_obj.get('strategy_blueprint')),
            goal_obj.get('execution_log', None),
            goal_obj.get('preferred_tier', 'tier1') # Add the new field
        ))
        con.commit()
        con.close()
    except Exception as e:
        logger.error(f"Failed to add goal to database: {e}")

def _tuple_to_goal_dict(goal_tuple: tuple) -> dict:
    """Helper to convert a database row to a goal dictionary."""
    if not goal_tuple: return None
    return {
        'goal_id': goal_tuple[0], 'goal': goal_tuple[1], 'plan': json.loads(goal_tuple[2]),
        'audit_critique': goal_tuple[3], 'status': goal_tuple[4],
        'strategy_blueprint': json.loads(goal_tuple[5]) if goal_tuple[5] else {},
        'execution_log': goal_tuple[6],
        'preferred_tier': goal_tuple[7] # Add the new field
    }

def get_active_goal() -> dict | None:
    """Fetches the first pending or in-progress goal from the database."""
    try:
        con = sqlite3.connect(DB_PATH, check_same_thread=False)
        cur = con.cursor()
        res = cur.execute("SELECT * FROM goals WHERE status IN ('pending', 'in-progress') ORDER BY goal_id ASC LIMIT 1")
        goal_tuple = res.fetchone()
        con.close()
        return _tuple_to_goal_dict(goal_tuple)
    except Exception as e:
        logger.error(f"Failed to get active goal from database: {e}")
        return None

def update_goal(goal_obj: dict):
    """Updates an existing goal in the database."""
    try:
        con = sqlite3.connect(DB_PATH, check_same_thread=False)
        cur = con.cursor()
        cur.execute("UPDATE goals SET plan = ?, status = ?, execution_log = ? WHERE goal_id = ?", (
            json.dumps(goal_obj.get('plan')),
            goal_obj.get('status'),
            goal_obj.get('execution_log'),
            goal_obj.get('goal_id')
        ))
        con.commit()
        con.close()
    except Exception as e:
        logger.error(f"Failed to update goal '{goal_obj.get('goal_id')}': {e}")

def update_goal_tier(goal_id: str, new_tier: str):
    """Updates only the preferred_tier of a specific goal."""
    try:
        con = sqlite3.connect(DB_PATH, check_same_thread=False)
        cur = con.cursor()
        cur.execute("UPDATE goals SET preferred_tier = ? WHERE goal_id = ?", (new_tier, goal_id))
        con.commit()
        con.close()
        logger.info(f"Updated preferred_tier for goal '{goal_id}' to '{new_tier}'.")
    except Exception as e:
        logger.error(f"Failed to update tier for goal '{goal_id}': {e}")

def get_archived_goals(page: int = 1, per_page: int = 10) -> list:
    """Fetches a paginated list of completed or failed goals from the archive."""
    offset = (page - 1) * per_page
    try:
        con = sqlite3.connect(DB_PATH, check_same_thread=False)
        cur = con.cursor()
        res = cur.execute("SELECT * FROM archive ORDER BY goal_id DESC LIMIT ? OFFSET ?", (per_page, offset))
        goals_list = [_tuple_to_goal_dict(t) for t in res.fetchall()]
        con.close()
        return goals_list
    except Exception as e:
        logger.error(f"Failed to get archived goals: {e}")
        return []

def get_active_goals() -> list:
    """Fetches all active goals (pending, in-progress, etc.), newest first."""
    try:
        con = sqlite3.connect(DB_PATH, check_same_thread=False)
        cur = con.cursor()
        
        # --- THIS IS THE FIX ---
        # Added 'awaiting_tier_decision' to the list of statuses
        res = cur.execute("SELECT * FROM goals WHERE status IN ('pending', 'in-progress', 'awaiting_input', 'paused', 'awaiting_tier_decision') ORDER BY goal_id DESC")
        # --- END FIX ---
        
        goals_list = [_tuple_to_goal_dict(t) for t in res.fetchall()]
        con.close()
        return goals_list
    except Exception as e:
        logger.error(f"Failed to get active goals: {e}")
        return []
    
def get_archived_goal_count() -> int:
    """Returns the total number of goals in the archive."""
    try:
        con = sqlite3.connect(DB_PATH, check_same_thread=False)
        cur = con.cursor()
        res = cur.execute("SELECT COUNT(*) FROM archive")
        count = res.fetchone()[0]
        con.close()
        return count
    except Exception as e:
        logger.error(f"Failed to get archived goal count: {e}")
        return 0
        
def get_goal_by_id(goal_id: str) -> dict | None:
    """Fetches a specific goal by its unique ID from the active goals table."""
    try:
        con = sqlite3.connect(DB_PATH, check_same_thread=False)
        cur = con.cursor()
        res = cur.execute("SELECT * FROM goals WHERE goal_id = ?", (goal_id,))
        goal_tuple = res.fetchone()
        con.close()
        return _tuple_to_goal_dict(goal_tuple)
    except Exception as e:
        logger.error(f"Failed to get goal by ID '{goal_id}': {e}")
        return None

def archive_goal(goal_id: str):
    """Moves a goal from the 'goals' table to the 'archive' table."""
    try:
        con = sqlite3.connect(DB_PATH, check_same_thread=False)
        cur = con.cursor()
        res = cur.execute("SELECT * FROM goals WHERE goal_id = ?", (goal_id,))
        goal_to_archive = res.fetchone()
        if goal_to_archive:
            # Ensure the tuple has the right number of elements (8)
            if len(goal_to_archive) < 8:
                goal_to_archive += (None,) * (8 - len(goal_to_archive))
            cur.execute("INSERT OR REPLACE INTO archive VALUES (?, ?, ?, ?, ?, ?, ?, ?)", goal_to_archive)
            cur.execute("DELETE FROM goals WHERE goal_id = ?", (goal_id,))
            con.commit()
    except Exception as e:
        logger.error(f"Failed to archive goal '{goal_id}': {e}")

def update_goal_status(goal_id: str, status: str):
    """Updates only the status of a specific goal."""
    try:
        con = sqlite3.connect(DB_PATH, check_same_thread=False)
        cur = con.cursor()
        cur.execute("UPDATE goals SET status = ? WHERE goal_id = ?", (status, goal_id))
        con.commit()
        con.close()
        logger.info(f"Updated status for goal '{goal_id}' to '{status}'.")
    except Exception as e:
        logger.error(f"Failed to update status for goal '{goal_id}': {e}")