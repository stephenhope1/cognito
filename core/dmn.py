import uuid
import os
from datetime import datetime, timedelta

from core.context import logger, gemini_client, memory_manager
from core.planner import orchestrate_planning
from utils.database import add_goal
from utils.calendar_client import get_upcoming_events


def creative_synthesis_loop(gemini_client_instance, memory_manager_instance):
    """
    The core creative loop of the DMN.
    - Brainstorms a new goal and uses the full Strategist -> Planner pipeline.
    """
    logger.info("   -> DMN: Starting creative synthesis...")

    try:
        with open('data/user_profile.json', 'r') as f:
            profile_str = f.read()
    except Exception as e:
        logger.error(f"   -> DMN ERROR: Could not load user profile. Aborting. {e}")
        return

    calendar_events = get_upcoming_events()
    calendar_str = calendar_events if calendar_events else "No upcoming events in the next 24 hours."

    all_memories = memory_manager_instance.collection.get()
    memory_sample = []
    if len(all_memories.get('ids', [])) > 5:
        import random
        sample_ids = random.sample(all_memories['ids'], 5)
        memory_sample_data = memory_manager_instance.collection.get(ids=sample_ids)
        memory_sample = memory_sample_data.get('documents', [])
    else:
        memory_sample = all_memories.get('documents', [])
    memories_str = "\n".join(f"- {mem}" for mem in memory_sample) if memory_sample else "No memories found."
    
    brainstorm_prompt = f"""
    Based on the following user profile, schedule, and recent memories, brainstorm one single, actionable goal that would be helpful for the user.
    Respond with only the goal as a single sentence.

    User Profile: {profile_str}
    Upcoming Schedule: {calendar_str}
    Recent Memories: {memories_str}
    """
    
    logger.info("   -> DMN: Brainstorming a new goal idea...")
    new_goal_idea = gemini_client_instance.ask_gemini(brainstorm_prompt, tier='tier1')
    
    if not new_goal_idea:
        logger.error("   -> DMN ERROR: Brainstorming did not produce a goal.")
        return

    new_goal_idea = new_goal_idea.strip()
    logger.info(f"   -> DMN: Generated Goal Idea: '{new_goal_idea}'")

    # MODIFIED: Use the central planning orchestrator for the new idea
    new_goal_obj = orchestrate_planning(new_goal_idea)

    if new_goal_obj:
        # The DMN should not create ambiguous goals that immediately require user input.
        if new_goal_obj['status'] == 'awaiting_input':
            logger.warning(f"   -> DMN: Generated goal '{new_goal_idea}' is too ambiguous, discarding.")
            return

        # Add the final unique ID and source before saving
        new_goal_obj['goal_id'] = f"dmn_goal_{uuid.uuid4()}"
        add_goal(new_goal_obj)
        logger.info(f"   -> DMN: Successfully added new proactive goal '{new_goal_obj['goal_id']}' to the database.")
    else:
        logger.error(f"   -> DMN ERROR: Failed to create a plan for the self-generated goal.")


def generate_eod_summary(memory_manager_instance, gemini_client_instance):
    """Generates an end-of-day summary based on memories from the last 24 hours."""
    logger.info("   -> EOD Summary: Starting summary generation...")
    summary = None

    try:
        all_memories = memory_manager_instance.collection.get(include=["metadatas", "documents"])
        now = datetime.now()
        one_day_ago = now - timedelta(days=1)
        
        recent_memories = []
        for i, metadata in enumerate(all_memories.get('metadatas', [])):
            timestamp_str = metadata.get('timestamp')
            if timestamp_str:
                timestamp = datetime.fromisoformat(timestamp_str)
                if timestamp > one_day_ago:
                    recent_memories.append(all_memories['documents'][i])

        if not recent_memories:
            logger.info("   -> EOD Summary: No recent memories found. Generating a 'no activity' report.")
            summary = "No significant activity or new memories were recorded in the last 24 hours."
        else:
            memories_str = "\n".join(f"- {mem}" for mem in recent_memories)
            prompt = f"""
            You are an AI assistant tasked with writing an end-of-day summary for your user.
            Based on the following list of your activities and completed tasks from the last 24 hours, write a concise briefing.
            
            Structure the summary with two sections:
            1.  **Key Accomplishments:** A bulleted list of the most important tasks you completed.
            2.  **Generated Insights:** A brief paragraph noting any new ideas or proactive tasks you generated (if any).

            Today's Activities:
            {memories_str}

            Now, please generate the end-of-day summary.
            """
            
            logger.info("   -> EOD Summary: Generating summary with Gemini...")
            summary = gemini_client_instance.ask_gemini(prompt, tier='tier1')

    except Exception as e:
        logger.error(f"   -> EOD Summary ERROR: Could not retrieve memories. {e}")
        summary = "An error occurred while trying to generate the daily summary."
        
    if not summary:
        logger.error("   -> EOD Summary ERROR: Failed to generate summary text for unknown reasons.")
        summary = "The summary could not be generated due to an unexpected error."
        
    try:
        report_dir = 'data/reports'
        os.makedirs(report_dir, exist_ok=True)
        report_filename = os.path.join(report_dir, f"{datetime.now().strftime('%Y-%m-%d')}_summary.md")
        
        with open(report_filename, 'w', encoding='utf-8') as f:
            f.write(f"# End-of-Day Summary for {datetime.now().strftime('%B %d, %Y')}\n\n")
            f.write(summary)
            
        logger.info(f"   -> EOD Summary: Successfully saved report to {report_filename}")
        
    except Exception as e:
        logger.error(f"   -> EOD Summary ERROR: Could not save the report. {e}")