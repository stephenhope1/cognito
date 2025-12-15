import uuid
import os
from datetime import datetime, timedelta
import json
from core.context import logger, gemini_client, memory_manager
from core.planner import orchestrate_planning
from utils.database import add_goal, get_recent_failed_goals, get_archived_goals, update_user_profile, get_active_goal
from utils.calendar_client import get_upcoming_events

def run_dmn_tasks(gemini_client_instance, memory_manager_instance):
    """
    The main DMN orchestrator. It runs when the agent is idle
    and decides which background task to perform, in order of priority.
    """
    # Ensure we are truly idle and not just in a 30s-loop
    if get_active_goal():
        logger.info("DMN: Agent is not idle (active goal found). DMN standing by.")
        return
        
    logger.info("--- DMN (Idle) Orchestrator Waking Up ---")
    
    # Priority 1: Learn from recent failures (Reflexion)
    if len(get_recent_failed_goals(limit=1)) > 0:
        logger.info("DMN: Triggering Reflexion Loop (P1)...")
        run_reflexion_loop(gemini_client_instance, memory_manager_instance)
        return # Only do one task per idle cycle

    # Priority 2: Infer user preferences (Profile Weaving)
    # We'll run this less often, e.g., if we haven't in a while.
    # (For now, we just check. We can add a timestamp check later).
    logger.info("DMN: Triggering User Profile Weaver (P2)...")
    run_user_profile_weaver(gemini_client_instance)
    
    # Priority 3: Synthesize new insights (Memory Weaving)
    logger.info("DMN: Triggering Memory Weaving Loop (P3)...")
    run_memory_weaving_loop(gemini_client_instance, memory_manager_instance)
    
    # Priority 4: Brainstorm a new task (Proactive Goal)
    logger.info("DMN: Triggering Creative Synthesis Loop (P4)...")
    creative_synthesis_loop(gemini_client_instance, memory_manager_instance)

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
    response = gemini_client_instance.ask_gemini(brainstorm_prompt, tier='tier1')
    
    if not response or not hasattr(response, 'text') or not response.text:
        logger.error("   -> DMN ERROR: Brainstorming did not produce a goal.")
        return

    new_goal_idea = response.text.strip()
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

def run_memory_weaving_loop(gemini_client_instance, memory_manager_instance):
    """
    Analyzes recent "fact" memories to synthesize new, higher-level "insights".
    This runs when the agent is idle and has new facts to process.
    """
    logger.info("   -> DMN: Starting memory weaving loop...")

    try:
        # 1. Check if we've run this task recently
        last_weave_mem = memory_manager_instance.collection.get(
            ids=["_internal_last_weave"],
            where={"type": "insight_timestamp"}
        )
        
        if last_weave_mem and last_weave_mem['ids']:
            last_weave_time = datetime.fromisoformat(last_weave_mem['metadatas'][0]['timestamp'])
            # Don't run more than once every 6 hours
            if (datetime.now() - last_weave_time) < timedelta(hours=6):
                logger.info("   -> DMN: Memory weaving ran recently. Standing by.")
                return

        # 2. Get the 20 most recent "fact" memories
        # We specifically exclude heuristics and insights
        recent_facts = memory_manager_instance.collection.query(
            query_texts=["user activity"], # Generic query to get recent items
            n_results=20,
            where={"type": {"$nin": ["heuristic", "insight", "insight_timestamp"]}}
        )

        fact_list = recent_facts.get('documents', [[]])[0]
        if len(fact_list) < 5: # Don't run if there aren't enough new facts
            logger.info(f"   -> DMN: Not enough new facts to synthesize ({len(fact_list)}). Standing by.")
            return

        logger.info(f"   -> DMN: Found {len(fact_list)} new facts to weave. Creating synthesis task.")
        facts_str = "\n".join(f"- {fact}" for fact in fact_list)

        # 3. Create the "Insight Synthesis" sub-goal
        synthesis_sub_goal = f"""
        You are an "Insight Synthesizer." Your task is to analyze a list of recent facts from your memory and "weave" them into new, higher-level insights.

        An "insight" is an emergent conclusion, pattern, or summary. It is *not* just a list of the facts.
        
        **RULES:**
        1.  Analyze all the facts provided.
        2.  Generate 1-3 new, high-level "Insight" memories.
        3.  Each insight MUST be a single, concise sentence.
        4.  Do *not* just summarize the facts. Find a *new angle*. (e.g., if facts are about restaurants and maps, an insight might be "Insight: The user is planning a trip.")
        5.  You MUST use the `add_memory` tool to save each new insight.
        
        **RECENT FACTS TO ANALYZE:**
        {facts_str}

        **YOUR PLAN:**
        1.  Formulate 1-3 new insight strings.
        2.  For each insight, call the `add_memory` tool. Use a unique `doc_id` (e.g., "insight_..."), and set the `metadata` to `{{"type": "insight"}}`.
        3.  Once all new insights are saved, provide a final text response summarizing what you learned.
        """

        # 4. Create a new goal object for the synthesis
        new_goal_obj = orchestrate_planning(
            user_goal=f"Synthesize {len(fact_list)} recent memories into new insights"
        )

        if not new_goal_obj:
            logger.error(f"   -> DMN ERROR: Failed to create a plan for the memory weaving task.")
            return
            
        # 5. Modify the plan to use our 'reactive_solve' goal
        new_goal_obj['plan'] = [{
            "step_id": 1,
            "dependencies": [],
            "tool_call": {
                "tool_name": "reactive_solve",
                "parameters": json.dumps({"sub_goal": synthesis_sub_goal})
            },
            "status": "pending",
            "output": None
        }]
        
        # 6. Add the synthesis goal to the database
        new_goal_obj['goal_id'] = f"dmn_weaving_{uuid.uuid4()}"
        new_goal_obj['preferred_tier'] = 'tier1' # This is a high-priority task
        add_goal(new_goal_obj)
        
        # 7. Save a new timestamp to memory so we don't run this again right away
        memory_manager_instance.add_memory(
            document=f"Memory weaving last performed at {datetime.now().isoformat()}",
            doc_id="_internal_last_weave",
            metadata={"type": "insight_timestamp", "timestamp": datetime.now().isoformat()}
        )
        
        logger.info(f"   -> DMN: Successfully added new memory weaving goal '{new_goal_obj['goal_id']}' to the database.")

    except Exception as e:
        logger.error(f"   -> DMN ERROR: An error occurred during the memory weaving loop: {e}", exc_info=True)

def run_user_profile_weaver(gemini_client_instance):
    """
    Analyzes past *successful* goals to infer user preferences
    and saves them to the user_profile table.
    """
    logger.info("   -> DMN: Starting user profile weaver...")

    try:
        # 1. Get the 10 most recent *completed* goals
        successful_goals = get_archived_goals(page=1, per_page=10)
        # Filter for only 'complete' status
        successful_goals = [g for g in successful_goals if g.get('status') == 'complete']

        if len(successful_goals) < 3:
            logger.info(f"   -> DMN: Not enough successful goals ({len(successful_goals)}) to analyze for profile.")
            return

        # 2. Format the goals for the prompt
        goal_list_str = "\n".join([f"- {g['goal']}" for g in successful_goals])

        # 3. Create the analysis prompt
        analysis_prompt = f"""
        You are a "User Profile Analyst" AI. Your job is to analyze a user's recently completed goals
        to infer their long-term interests, preferences, and recurring tasks.

        **Recently Completed Goals:**
        {goal_list_str}

        **INSTRUCTIONS:**
        1.  Analyze the list of goals for recurring themes or topics.
        2.  Generate 1-3 concise "insights" about the user's preferences.
        3.  Format your response as a JSON object, where each key is the insight (e.g., "Interests")
            and each value is the observation (e.g., "User is frequently interested in Canadian law and real estate.").
        
        **Example Output:**
        {{
          "primary_interest": "User appears to be a legal professional specializing in Canadian case law.",
          "secondary_interest": "User is also planning a trip, focusing on high-end dining."
        }}

        Now, generate the JSON object based on the goals provided.
        """

        response = gemini_client_instance.ask_gemini(
            analysis_prompt, 
            tier='tier1',
            response_schema={"type": "object", "additionalProperties": {"type": "string"}}
        )

        if response and hasattr(response, 'parsed'):
            insights = response.parsed
            if not insights:
                logger.warning("   -> DMN: User profile analysis returned no insights.")
                return

            # 4. Save each new insight to the database
            for key, value in insights.items():
                # We use 'dmn_inference' as the source
                update_user_profile(key, value, "dmn_inference")
            
            logger.info(f"   -> DMN: Successfully updated user profile with {len(insights)} new insights.")

        else:
            logger.error("   -> DMN ERROR: Failed to generate user profile insights.")

    except Exception as e:
        logger.error(f"   -> DMN ERROR: An error occurred during the user profile weaving loop: {e}", exc_info=True)

def run_reflexion_loop(gemini_client_instance, memory_manager_instance):
    """
    The core "Reflexion" loop of the DMN.
    - Creates a new "post-mortem" goal to analyze a past failure.
    """
    logger.info("   -> DMN: Starting reflexion loop...")

    # 1. Get the last failed goal
    failed_goals = get_recent_failed_goals(limit=1)
    if not failed_goals:
        logger.info("   -> DMN: No new failed goals found to analyze.")
        return

    goal_to_process = failed_goals[0]
    goal_id = goal_to_process['goal_id']

    # 2. Check if we've already created a heuristic for this failure
    # We use the failed goal's ID as the memory ID
    existing_heuristic = memory_manager_instance.collection.get(
        ids=[goal_id],
        where={"type": "heuristic"}
    )
    
    if existing_heuristic and existing_heuristic.get('ids'):
        logger.info(f"   -> DMN: Failed goal '{goal_id}' has already been analyzed. Standing by.")
        return

    logger.info(f"   -> DMN: New failed goal '{goal_id}' found. Creating post-mortem task.")

    # 3. Create the "post-mortem" sub-goal
    # We'll pass the failed goal's data as context
    failed_goal_data = {
        "original_goal": goal_to_process['goal'],
        "failed_plan": goal_to_process['plan'],
        "error_log": goal_to_process.get('execution_log', 'No log found.')
    }
    
    post_mortem_sub_goal = f"""
    You are a 'Reflexion Agent'. Your task is to perform a post-mortem on a failed agent task.
    Your goal is to determine the *root cause* of the failure and write a new, actionable "heuristic" (a lesson or rule of thumb) that the Planner AI can use in the future.

    You have access to all your tools, including `read_internal_file`, to read your own code (like `core/agent_profile.py` or `core/planner.py`) to find the bug.
    
    **FAILED TASK CONTEXT:**
    {json.dumps(failed_goal_data, indent=2)}

    **YOUR POST-MORTEM PLAN:**
    1.  Analyze the failed plan and error log. What went wrong?
    2.  If the cause is a flaw in your own logic, use `read_internal_file` to read your agent profile and code to find the source of the bad logic.
    3.  Formulate a concise, one-sentence heuristic (e.g., "Heuristic: The Planner must not...").
    4.  Save this new heuristic to memory using `add_memory`.
    5.  Finally, return a summary of your findings.
    """
    
    # 4. Create a new goal object for the post-mortem
    # We will use the 'existing_context_str' to pass the data
    new_goal_obj = orchestrate_planning(
        user_goal=f"Post-mortem for failed goal {goal_id}",
        existing_context_str=json.dumps(failed_goal_data, indent=2)
    )

    if not new_goal_obj:
        logger.error(f"   -> DMN ERROR: Failed to create a plan for the post-mortem task.")
        return
        
    # 5. Modify the plan to use our 'reactive_solve' goal
    # We replace the auto-generated plan with our own single, powerful step
    new_goal_obj['plan'] = [{
        "step_id": 1,
        "dependencies": [],
        "tool_call": {
            "tool_name": "reactive_solve",
            "parameters": json.dumps({"sub_goal": post_mortem_sub_goal})
        },
        "status": "pending",
        "output": None
    }]
    
    # 6. Add the post-mortem goal to the database
    new_goal_obj['goal_id'] = f"dmn_reflexion_{uuid.uuid4()}"
    new_goal_obj['preferred_tier'] = 'tier1' # This is a high-priority task
    add_goal(new_goal_obj)
    
    # 7. As a safeguard, save a *simple* heuristic to memory now,
    # so we don't try to analyze this same failure again.
    memory_manager_instance.add_memory(
        document=f"A post-mortem task was created for failed goal {goal_id}.",
        doc_id=goal_id,
        metadata={"type": "heuristic", "source_goal": goal_id}
    )
    
    logger.info(f"   -> DMN: Successfully added new post-mortem goal '{new_goal_obj['goal_id']}' to the database.")

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
            response = gemini_client_instance.ask_gemini(prompt, tier='tier1')

            if response and hasattr(response, 'text'):
                summary = response.text
            else:
                summary = None

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