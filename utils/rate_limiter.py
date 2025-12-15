import datetime
from datetime import timedelta
from .logger import logger
from .database import check_rate_limit_db, get_rate_limit_usage_db

class RateLimitTracker:
    """
    A stateless wrapper around the database-backed rate limit logic.
    Safe to instantiate in multiple processes (e.g., Voice Process and Main Process).
    """

    def __init__(self):
        self.limits = {
            'tier1': {'rpm': 2, 'rpd': 50},
            'tier2': {'rpm': 10, 'rpd': 250},
            'tier3': {'rpm': 15, 'rpd': 1000},
        }

    def _get_current_pt_time(self):
        """Helper to get current time in PT (UTC-8)."""
        return datetime.datetime.utcnow() - timedelta(hours=8)

    def get_time_elapsed_percentage(self) -> float:
        """
        Calculates the percentage of the current day that has elapsed in PST.
        Used by the DMN to calculate 'use-it-or-lose-it' surplus.
        """
        now_pt = self._get_current_pt_time()
        total_seconds_in_day = 24 * 60 * 60
        seconds_elapsed = (now_pt.hour * 3600) + (now_pt.minute * 60) + now_pt.second
        
        return (seconds_elapsed / total_seconds_in_day) * 100

    def check_and_increment(self, tier: str) -> bool:
        """
        Checks if a call can be made using the database.
        """
        if tier not in self.limits:
            logger.error(f"Error: Tier '{tier}' is not a valid tier.")
            return False
        
        rpm = self.limits[tier]['rpm']
        rpd = self.limits[tier]['rpd']
        
        # Delegate to the atomic DB function (safe across processes)
        allowed = check_rate_limit_db(tier, rpm, rpd)
        
        if allowed:
            # --- CHANGE: Use debug instead of info to reduce noise ---
            logger.debug(f"RateLimit: Call allowed for {tier} (RPM: {rpm}, RPD: {rpd})")
        else:
            logger.warning(f"RateLimit: Call BLOCKED for {tier}")
            
        return allowed

    def get_daily_usage_percentage(self, tier: str) -> float:
        """Calculates the current daily usage percentage via the DB."""
        if tier not in self.limits:
            return 0.0
        
        rpd = self.limits[tier]['rpd']
        return get_rate_limit_usage_db(tier, rpd)

if __name__ == '__main__':
    # Simple self-test
    from utils.database import initialize_database
    initialize_database()
    
    tracker = RateLimitTracker()
    logger.info("Testing DB-backed Rate Limiter...")
    
    if tracker.check_and_increment('tier1'):
        logger.info("Tier 1 call allowed.")
    else:
        logger.info("Tier 1 call blocked.")