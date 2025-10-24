import sqlite3
import json
from logs.logger import logger

DB_PATH = 'data/tasks.sqlite'

def initialize_database():
    """Creates the database and both the 'goals' and 'archive' tables."""
    try:
        con = sqlite3.connect(DB_PATH, check_same_thread=False)
        cur = con.cursor()
        # MODIFIED: Added strategy_blueprint TEXT column
        cur.execute('''
            CREATE TABLE IF NOT EXISTS goals (
                goal_id TEXT PRIMARY KEY,
                goal TEXT, plan TEXT, audit_critique TEXT, status TEXT,
                strategy_blueprint TEXT
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS archive (
                goal_id TEXT PRIMARY KEY,
                goal TEXT, plan TEXT, audit_critique TEXT, status TEXT,
                strategy_blueprint TEXT
            )
        ''')
        con.commit()
        con.close()
        logger.info("Database and tables initialized successfully (with strategy_blueprint).")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")

def add_goal(goal_obj: dict):
    """Adds a new goal object to the database."""
    try:
        con = sqlite3.connect(DB_PATH, check_same_thread=False)
        cur = con.cursor()
        cur.execute("INSERT INTO goals VALUES (?, ?, ?, ?, ?, ?)", (
            goal_obj.get('goal_id'),
            goal_obj.get('goal'),
            json.dumps(goal_obj.get('plan')),
            goal_obj.get('audit_critique'),
            goal_obj.get('status'),
            json.dumps(goal_obj.get('strategy_blueprint')) # Store as a JSON string
        ))
        con.commit()
        con.close()
        logger.info(f"Successfully added goal '{goal_obj.get('goal_id')}' to database.")
    except Exception as e:
        logger.error(f"Failed to add goal to database: {e}")

def _tuple_to_goal_dict(goal_tuple: tuple) -> dict:
    """Helper to convert a database row tuple to a goal dictionary."""
    if not goal_tuple:
        return None
    return {
        'goal_id': goal_tuple[0], 'goal': goal_tuple[1],
        'plan': json.loads(goal_tuple[2]), 'audit_critique': goal_tuple[3],
        'status': goal_tuple[4],
        'strategy_blueprint': json.loads(goal_tuple[5]) # Load from JSON string
    }

def get_active_goal() -> dict | None:
    """Fetches the first pending or in-progress goal from the database."""
    try:
        con = sqlite3.connect(DB_PATH, check_same_thread=False)
        cur = con.cursor()
        res = cur.execute("SELECT * FROM goals WHERE status IN ('pending', 'in-progress', 'awaiting_input') ORDER BY goal_id LIMIT 1")
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
        cur.execute("UPDATE goals SET goal = ?, plan = ?, audit_critique = ?, status = ?, strategy_blueprint = ? WHERE goal_id = ?", (
            goal_obj.get('goal'), json.dumps(goal_obj.get('plan')),
            goal_obj.get('audit_critique'), goal_obj.get('status'),
            json.dumps(goal_obj.get('strategy_blueprint')),
            goal_obj.get('goal_id')
        ))
        con.commit()
        con.close()
    except Exception as e:
        logger.error(f"Failed to update goal '{goal_obj.get('goal_id')}': {e}")

def get_active_goals() -> list:
    """Fetches all pending or in-progress goals from the database."""
    try:
        con = sqlite3.connect(DB_PATH, check_same_thread=False)
        cur = con.cursor()
        res = cur.execute("SELECT * FROM goals WHERE status IN ('pending', 'in-progress', 'awaiting_input') ORDER BY goal_id DESC")
        goals_list = [_tuple_to_goal_dict(t) for t in res.fetchall()]
        con.close()
        return goals_list
    except Exception as e:
        logger.error(f"Failed to get active goals: {e}")
        return []

def get_archived_goals() -> list:
    """Fetches all completed or failed goals from the archive."""
    try:
        con = sqlite3.connect(DB_PATH, check_same_thread=False)
        cur = con.cursor()
        res = cur.execute("SELECT * FROM archive ORDER BY goal_id DESC LIMIT 20")
        goals_list = [_tuple_to_goal_dict(t) for t in res.fetchall()]
        con.close()
        return goals_list
    except Exception as e:
        logger.error(f"Failed to get archived goals: {e}")
        return []
        
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
    logger.info(f"Archiving goal '{goal_id}'...")
    try:
        con = sqlite3.connect(DB_PATH, check_same_thread=False)
        cur = con.cursor()
        res = cur.execute("SELECT * FROM goals WHERE goal_id = ?", (goal_id,))
        goal_to_archive = res.fetchone()
        if goal_to_archive:
            # MODIFIED: The query now includes the 6th column
            cur.execute("INSERT OR REPLACE INTO archive VALUES (?, ?, ?, ?, ?, ?)", goal_to_archive)
            cur.execute("DELETE FROM goals WHERE goal_id = ?", (goal_id,))
            con.commit()
            logger.info(f"Successfully archived goal '{goal_id}'.")
        con.close()
    except Exception as e:
        logger.error(f"Failed to archive goal '{goal_id}': {e}")