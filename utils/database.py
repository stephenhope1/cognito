import sqlite3
import json
import time
import functools
from .logger import logger
from datetime import datetime

DB_PATH = 'data/tasks.sqlite'

def retry_db_op(max_retries=5, base_delay=0.1):
    """
    Decorator to retry database operations on locking errors.
    Uses exponential backoff.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            retries = 0
            while retries < max_retries:
                try:
                    return func(*args, **kwargs)
                except sqlite3.OperationalError as e:
                    if "locked" in str(e).lower():
                        retries += 1
                        sleep_time = base_delay * (2 ** retries)
                        logger.warning(f"DB LOCKED: Retrying {func.__name__} in {sleep_time:.2f}s... ({retries}/{max_retries})")
                        time.sleep(sleep_time)
                    else:
                        raise e
                except Exception as e:
                    logger.error(f"Database error in {func.__name__}: {e}")
                    raise e
            logger.error(f"DB FAILED: {func.__name__} failed after {max_retries} retries due to locks.")
            return None
        return wrapper
    return decorator

@retry_db_op()
def initialize_database():
    """Creates the database tables and enables WAL mode for concurrency."""
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    
    # --- CRITICAL: Enable Write-Ahead Logging (WAL) ---
    # This allows readers and writers to coexist, preventing
    # the Voice process from blocking the Orchestrator.
    con.execute("PRAGMA journal_mode=WAL;")
    
    cur = con.cursor()
    
    # 1. Goals Table
    cur.execute('''
        CREATE TABLE IF NOT EXISTS goals (
            goal_id TEXT PRIMARY KEY, goal TEXT, plan TEXT, 
            audit_critique TEXT, status TEXT, strategy_blueprint TEXT,
            execution_log TEXT, preferred_tier TEXT,
            replan_count INTEGER DEFAULT 0 
        )
    ''')
    
    # 2. Archive Table
    cur.execute('''
        CREATE TABLE IF NOT EXISTS archive (
            goal_id TEXT PRIMARY KEY, goal TEXT, plan TEXT,
            audit_critique TEXT, status TEXT, strategy_blueprint TEXT,
            execution_log TEXT, preferred_tier TEXT,
            replan_count INTEGER 
        )
    ''')
    
    # 3. User Profile Table
    cur.execute('''
        CREATE TABLE IF NOT EXISTS user_profile (
            key TEXT PRIMARY KEY,
            value TEXT,
            source TEXT,
            timestamp TEXT
        )
    ''')
    
    # 4. NEW: Rate Limits Table
    # Stores timestamps of API calls to replace the JSON file
    cur.execute('''
        CREATE TABLE IF NOT EXISTS rate_limits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tier TEXT NOT NULL,
            timestamp REAL NOT NULL
        )
    ''')
    
    # Index for faster lookups on rate limit checks
    cur.execute("CREATE INDEX IF NOT EXISTS idx_rate_tier_time ON rate_limits(tier, timestamp);")

    con.commit()
    con.close()
    logger.info("Database initialized (WAL Mode Enabled).")

# --- RATE LIMITER FUNCTIONS (NEW) ---

@retry_db_op()
def check_rate_limit_db(tier: str, rpm_limit: int, rpd_limit: int) -> bool:
    """
    Atomically checks and increments the rate limit counter.
    Returns True if allowed, False if blocked.
    """
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = con.cursor()
    now = time.time()
    
    # 1. Prune old records (cleanup) - Optional, can be done periodically
    # For performance, we might only do this occasionally, but for safety we do it now.
    # We keep records for 24 hours to calculate RPD.
    one_day_ago = now - 86400
    cur.execute("DELETE FROM rate_limits WHERE timestamp < ?", (one_day_ago,))
    
    # 2. Check RPM (Requests Per Minute)
    one_minute_ago = now - 60
    cur.execute("SELECT COUNT(*) FROM rate_limits WHERE tier = ? AND timestamp > ?", (tier, one_minute_ago))
    rpm_count = cur.fetchone()[0]
    
    if rpm_count >= rpm_limit:
        con.close()
        return False
    
    # 3. Check RPD (Requests Per Day) - Simple 24h sliding window for robustness
    cur.execute("SELECT COUNT(*) FROM rate_limits WHERE tier = ? AND timestamp > ?", (tier, one_day_ago))
    rpd_count = cur.fetchone()[0]
    
    if rpd_count >= rpd_limit:
        con.close()
        return False
    
    # 4. Allow: Insert new record
    cur.execute("INSERT INTO rate_limits (tier, timestamp) VALUES (?, ?)", (tier, now))
    con.commit()
    con.close()
    return True

@retry_db_op()
def get_rate_limit_usage_db(tier: str, rpd_limit: int) -> float:
    """Calculates the daily usage percentage for a tier."""
    if rpd_limit == 0: return 100.0
    
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = con.cursor()
    now = time.time()
    one_day_ago = now - 86400
    
    cur.execute("SELECT COUNT(*) FROM rate_limits WHERE tier = ? AND timestamp > ?", (tier, one_day_ago))
    count = cur.fetchone()[0]
    con.close()
    
    return (count / rpd_limit) * 100.0

# --- EXISTING FUNCTIONS (Hardened with @retry_db_op) ---

@retry_db_op()
def get_goal_status_by_id(goal_id: str) -> str | None:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = con.cursor()
    res = cur.execute("SELECT status FROM goals WHERE goal_id = ?", (goal_id,))
    status_tuple = res.fetchone()
    
    if status_tuple:
        con.close()
        return status_tuple[0]
    
    res_archive = cur.execute("SELECT status FROM archive WHERE goal_id = ?", (goal_id,))
    status_tuple_archive = res_archive.fetchone()
    con.close()
    
    return status_tuple_archive[0] if status_tuple_archive else None

@retry_db_op()
def add_goal(goal_obj: dict):
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = con.cursor()
    cur.execute("INSERT INTO goals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
        goal_obj.get('goal_id'), goal_obj.get('goal'), json.dumps(goal_obj.get('plan')),
        goal_obj.get('audit_critique'), goal_obj.get('status'),
        json.dumps(goal_obj.get('strategy_blueprint')),
        goal_obj.get('execution_log', None),
        goal_obj.get('preferred_tier', 'tier1'),
        goal_obj.get('replan_count', 0)
    ))
    con.commit()
    con.close()

def _tuple_to_goal_dict(goal_tuple: tuple) -> dict:
    if not goal_tuple: return None
    return {
        'goal_id': goal_tuple[0], 'goal': goal_tuple[1], 'plan': json.loads(goal_tuple[2]),
        'audit_critique': goal_tuple[3], 'status': goal_tuple[4],
        'strategy_blueprint': json.loads(goal_tuple[5]) if goal_tuple[5] else {},
        'execution_log': goal_tuple[6],
        'preferred_tier': goal_tuple[7],
        'replan_count': goal_tuple[8]
    }

@retry_db_op()
def get_active_goal() -> dict | None:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = con.cursor()
    res = cur.execute("SELECT * FROM goals WHERE status IN ('pending', 'in-progress', 'awaiting_replan') ORDER BY goal_id ASC LIMIT 1")
    goal_tuple = res.fetchone()
    con.close()
    return _tuple_to_goal_dict(goal_tuple)

@retry_db_op()
def update_goal(goal_obj: dict):
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

@retry_db_op()
def update_goal_tier(goal_id: str, new_tier: str):
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = con.cursor()
    cur.execute("UPDATE goals SET preferred_tier = ? WHERE goal_id = ?", (new_tier, goal_id))
    con.commit()
    con.close()

@retry_db_op()
def get_recent_failed_goals(limit: int = 5) -> list:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = con.cursor()
    res = cur.execute("SELECT * FROM archive WHERE status = 'failed' ORDER BY goal_id DESC LIMIT ?", (limit,))
    goals_list = [_tuple_to_goal_dict(t) for t in res.fetchall()]
    con.close()
    return goals_list

@retry_db_op()
def get_archived_goals(page: int = 1, per_page: int = 10) -> list:
    offset = (page - 1) * per_page
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = con.cursor()
    res = cur.execute("SELECT * FROM archive ORDER BY goal_id DESC LIMIT ? OFFSET ?", (per_page, offset))
    goals_list = [_tuple_to_goal_dict(t) for t in res.fetchall()]
    con.close()
    return goals_list

@retry_db_op()
def get_active_goals() -> list:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = con.cursor()
    res = cur.execute("SELECT * FROM goals WHERE status IN ('pending', 'in-progress', 'awaiting_input', 'paused', 'awaiting_tier_decision', 'awaiting_replan') ORDER BY goal_id DESC")
    goals_list = [_tuple_to_goal_dict(t) for t in res.fetchall()]
    con.close()
    return goals_list

@retry_db_op()
def get_archived_goal_count() -> int:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = con.cursor()
    res = cur.execute("SELECT COUNT(*) FROM archive")
    count = res.fetchone()[0]
    con.close()
    return count

@retry_db_op()
def get_goal_by_id(goal_id: str) -> dict | None:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = con.cursor()
    res = cur.execute("SELECT * FROM goals WHERE goal_id = ?", (goal_id,))
    goal_tuple = res.fetchone()
    con.close()
    return _tuple_to_goal_dict(goal_tuple)

@retry_db_op()
def archive_goal(goal_id: str):
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = con.cursor()
    res = cur.execute("SELECT * FROM goals WHERE goal_id = ?", (goal_id,))
    goal_to_archive = res.fetchone()
    if goal_to_archive:
        # Ensure the tuple has the right number of elements (9)
        if len(goal_to_archive) < 9:
            goal_to_archive += (None,) * (9 - len(goal_to_archive))
        cur.execute("INSERT OR REPLACE INTO archive VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", goal_to_archive)
        cur.execute("DELETE FROM goals WHERE goal_id = ?", (goal_id,))
        con.commit()
    con.close()

@retry_db_op()
def update_goal_status(goal_id: str, status: str):
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = con.cursor()
    cur.execute("UPDATE goals SET status = ? WHERE goal_id = ?", (status, goal_id))
    con.commit()
    con.close()

@retry_db_op()
def update_user_profile(key: str, value: str, source: str):
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = con.cursor()
    timestamp = datetime.now().isoformat()
    cur.execute("INSERT OR REPLACE INTO user_profile (key, value, source, timestamp) VALUES (?, ?, ?, ?)",
                (key, value, source, timestamp))
    con.commit()
    con.close()

@retry_db_op()
def get_user_profile() -> dict:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = con.cursor()
    res = cur.execute("SELECT key, value FROM user_profile")
    profile = {}
    for row in res.fetchall():
        profile[row[0]] = row[1]
    con.close()
    return profile