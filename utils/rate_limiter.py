import time
import json
import os
from datetime import datetime
from logs.logger import logger

STATE_FILE_PATH = 'data/ratelimit_state.json'

class RateLimitTracker:
    """A class to track and enforce API rate limits, with persistent state."""

    def __init__(self):
        self.limits = {
            'tier1': {'rpm': 5, 'rpd': 100},
            'tier2': {'rpm': 10, 'rpd': 250},
            'tier3': {'rpm': 15, 'rpd': 1000},
        }
        self._load_state() # MODIFIED: Load state on initialization

    def _load_state(self):
        """Loads the last known state from the JSON file."""
        try:
            with open(STATE_FILE_PATH, 'r') as f:
                state = json.load(f)
            self.call_timestamps = state.get('call_timestamps', {tier: [] for tier in self.limits})
            self.daily_counts = state.get('daily_counts', {tier: 0 for tier in self.limits})
            self.last_reset_day = state.get('last_reset_day', datetime.now().day)
            logger.info("RateLimitTracker state loaded successfully.")
        except (FileNotFoundError, json.JSONDecodeError):
            logger.warning("No valid rate limit state file found. Initializing with fresh state.")
            # Initialize with default empty state if file doesn't exist or is corrupt
            self.call_timestamps = {tier: [] for tier in self.limits}
            self.daily_counts = {tier: 0 for tier in self.limits}
            self.last_reset_day = datetime.now().day

    def _save_state(self):
        """Saves the current state to the JSON file."""
        state = {
            'call_timestamps': self.call_timestamps,
            'daily_counts': self.daily_counts,
            'last_reset_day': self.last_reset_day
        }
        with open(STATE_FILE_PATH, 'w') as f:
            json.dump(state, f)

    def _reset_if_new_day(self):
        """Resets daily counts if the day has changed."""
        current_day = datetime.now().day
        if current_day != self.last_reset_day:
            logger.info("--- New day detected, resetting daily API limits. ---")
            self.daily_counts = {tier: 0 for tier in self.limits}
            self.last_reset_day = current_day
            self._save_state() # MODIFIED: Save state after resetting

    def check_and_increment(self, tier: str) -> bool:
        """
        Checks if a call can be made. If so, increments counters, saves state, and returns True.
        """
        if tier not in self.limits:
            logger.error(f"Error: Tier '{tier}' is not a valid tier.")
            return False

        self._reset_if_new_day()
        
        current_time = time.time()
        
        # RPD Check
        if self.daily_counts[tier] >= self.limits[tier]['rpd']:
            logger.warning(f"RATE LIMIT: Daily limit reached for {tier}.")
            return False

        # RPM Check
        self.call_timestamps[tier] = [ts for ts in self.call_timestamps[tier] if current_time - ts < 60]
        if len(self.call_timestamps[tier]) >= self.limits[tier]['rpm']:
            logger.warning(f"RATE LIMIT: Minute limit reached for {tier}.")
            return False
            
        # All checks passed, increment and allow the call
        self.call_timestamps[tier].append(current_time)
        self.daily_counts[tier] += 1
        
        logger.info(f"Call allowed for {tier}. "
                    f"RPM: {len(self.call_timestamps[tier])}/{self.limits[tier]['rpm']}, "
                    f"RPD: {self.daily_counts[tier]}/{self.limits[tier]['rpd']}")
        
        self._save_state() # MODIFIED: Save state after every successful call
        return True


    def get_daily_usage_percentage(self, tier: str) -> float:
        """Calculates the current daily usage percentage for a given tier."""
        if tier not in self.limits:
            return 0.0
        
        self._reset_if_new_day()
        
        limit = self.limits[tier]['rpd']
        if limit == 0:
            return 100.0 # Avoid division by zero
        
        return (self.daily_counts[tier] / limit) * 100


if __name__ == '__main__':
    logger.info("--- Testing RateLimitTracker ---")
    tracker = RateLimitTracker()
    
    # Test Tier 3 RPM limit
    logger.info("\n--- Testing Tier 3 (15 RPM) ---")
    for i in range(17):
        logger.info(f"Attempting call {i+1}...", end=" ")
        tracker.check_and_increment('tier3')
        time.sleep(0.5) # Quick pause between calls

    # Test Tier 1 RPD limit by manually setting it
    logger.info("\n--- Testing Tier 1 (100 RPD) ---")
    tracker.daily_counts['tier1'] = 99
    tracker.check_and_increment('tier1') # Should be allowed
    tracker.check_and_increment('tier1') # Should be blocked