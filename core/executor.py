from core.context import logger
import json

def run_executor(user_goal: str, full_plan: list, strategy_blueprint: dict, context_map: dict, current_step_prompt: str, gemini_client, task_type: str) -> str:
    """
    Takes a simple plan step and refines it into a hyper-detailed,
    context-aware, and executable prompt for a final LLM.
    This acts as the "tactical layer" of the agent.
    
    Args:
        ...
        task_type (str): One of ['refine_prompt', 'refine_subgoal', 'refine_query']
    """
    logger.info(f"EXECUTOR: Refining task (type: {task_type}) for step: '{current_step_prompt}'")

    context_str = "\n".join([f"CONTEXT FOR `{k}`: {v}" for k, v in context_map.items()])
    plan_str = json.dumps(full_plan, indent=2)

    # --- NEW: Persona and Task are now dynamic ---
    persona = ""
    task_instructions = ""

    if task_type == "refine_prompt":
        persona = "You are the 'Executor,' a specialist AI component in a larger agentic framework. Your sole purpose is to take a *simple, high-level plan step* (a 'prompt') and *refine it* into a *detailed, low-level, executable prompt* for a final AI model."
        task_instructions = f"""
        **YOUR TASK**
        Rewrite the 'CURRENT SIMPLE STEP' into an optimized, imperative prompt for the final LLM.
        1.  Your new prompt must adopt an expert persona.
        2.  It must *weave in* the relevant data from the 'RESOLVED CONTEXT' block.
        3.  It must be a direct command, not a description.
        4.  If the simple step implies using a tool (like "search Google"), you MUST include the instruction to "use google_search" in your refined prompt.
        5.  Your response MUST be only the rewritten prompt text and nothing else.
        
        **CURRENT SIMPLE STEP (to be refined):**
        "{current_step_prompt}"
        """
    elif task_type == "refine_subgoal":
        persona = "You are the 'Executor,' a specialist AI component. Your sole purpose is to take a *simple, high-level sub-goal* and *refine it* into a *detailed, actionable sub-goal* for the ReAct (Problem Solver) agent."
        task_instructions = f"""
        **YOUR TASK**
        Rewrite the 'CURRENT SIMPLE SUB-GOAL' into a detailed, multi-point set of instructions.
        1.  Your new sub-goal must be an imperative command.
        2.  It must *explicitly* tell the ReAct agent to use its tools (like `Google Search`).
        3.  It must *weave in* the relevant data from the 'RESOLVED CONTEXT' block.
        4.  It must be clear about the *final output* it expects the ReAct agent to produce.
        5.  Your response MUST be only the rewritten sub-goal string and nothing else.
        
        **CURRENT SIMPLE SUB-GOAL (to be refined):**
        "{current_step_prompt}"
        """
    elif task_type == "refine_query":
        persona = "You are a 'Search Query Expert.' Your sole purpose is to take a simple, conversational query and rewrite it into an expert-level, highly-effective Google search query string."
        task_instructions = f"""
        **YOUR TASK**
        Rewrite the 'SIMPLE QUERY' into an expert-level Google search query.
        1.  Use boolean operators (AND, OR, NOT), site-specific searches (site:), and other advanced syntax.
        2.  Your output MUST be *only* the new, single-line query string and nothing else.
        
        **SIMPLE QUERY (to be refined):**
        "{current_step_prompt}"
        """

    prompt = f"""
    {persona}
    You must be meticulous. The final component you are prompting has NO access to the full plan or context map unless you embed it.

    ---
    **CONTEXT (FOR YOUR USE ONLY)**

    **The User's Overall Goal:**
    "{user_goal}"

    **The Full Strategic Plan:**
    {plan_str}

    **Resolved Context from Previous Steps:**
    {context_str if context_str else "No output from previous steps."}
    ---

    {task_instructions}

    ---
    **--- EXAMPLES OF YOUR OUTPUT ---**

    **--- EXAMPLE 1: `task_type="refine_query"` ---**

    USER_INPUT:
    You are a 'Search Query Expert.'...
    **CONTEXT (FOR YOUR USE ONLY)**
    **The User's Overall Goal:** "write a 15 page, comprehensive report detailing recent (2025) supreme court of canada cases"
    ...
    **YOUR TASK**
    ...
    **SIMPLE QUERY (to be refined):**
    "most significant Supreme Court of Canada cases decided in 2025"

    MODEL_OUTPUT:
    ("Supreme Court of Canada" OR SCC) AND (judgments OR decisions OR rulings) AND 2025 AND ("landmark" OR "significant" OR "key" OR "notable") AND ("constitutional law" OR "Charter of Rights" OR "Aboriginal law") (site:scc-csc.ca OR site:canlii.org OR site:thelawyersdaily.ca OR slaw.ca)

    **--- EXAMPLE 2: `task_type="refine_subgoal"` ---**

    USER_INPUT:
    You are the 'Executor,' a specialist AI component...
    **CONTEXT (FOR YOUR USE ONLY)**
    **The User's Overall Goal:** "write a 15 page, comprehensive report..."
    **Resolved Context from Previous Steps:**
    CONTEXT FOR `[output_of_step_1]`: "The Supreme Court... a notable ruling... *R. v. J.W., 2025 SCC 16*... *Opsis Airport Services Inc. v. Quebec...*"
    ...
    **YOUR TASK**
    ...
    **CURRENT SIMPLE SUB-GOAL (to be refined):**
    "Analyze the search results from [output_of_step_1] and identify the top 5 cases."

    MODEL_OUTPUT:
    Act as a meticulous legal researcher. Your sub-goal is to analyze the following text and extract the top 5 most significant case names.

    **Source Text:**
    "The Supreme Court... a notable ruling... *R. v. J.W., 2025 SCC 16*... *Opsis Airport Services Inc. v. Quebec...*"

    You must use your best judgment to determine "significance". You must then call the `finish` tool, returning *only* a JSON list of the case names in the `answer` parameter. Example: `{{"answer": "[\"Case 1 v. Case 2\", \"Case 3 v. Case 4\"]"}}`

    **--- EXAMPLE 3: `task_type="refine_prompt"` ---**

    USER_INPUT:
    You are the 'Executor,' a specialist AI component...
    **CONTEXT (FOR YOUR USE ONLY)**
    **The User's Overall Goal:** "write a 15 page, comprehensive report..."
    **Resolved Context from Previous Steps:**
    CONTEXT FOR `[output_of_step_2]`: "Summary of R. v. Morrison: This case was about... [full details]..."
    CONTEXT FOR `[output_of_step_3]`: "Summary of Quebec v. Senneville: This case was about... [full details]..."
    ...
    **YOUR TASK**
    ...
    **CURRENT SIMPLE STEP (to be refined):**
    "Synthesize the research from [output_of_step_2] and [output_of_step_3] into a final report."

    MODEL_OUTPUT:
    Act as a senior legal scholar. Your task is to write a comprehensive, multi-page report by synthesizing the two detailed case summaries provided below.

    Your report must have a clear introduction, a dedicated analysis section for each case, and a concluding section that discusses any overarching themes or trends.

    **Case Summary 1: R. v. Morrison**
    "Summary of R. v. Morrison: This case was about... [full details]..."

    **Case Summary 2: Quebec v. Senneville**
    "Summary of Quebec v. Senneville: This case was about... [full details]..."

    Produce only the final, formatted report.
    ---
    """

    response = gemini_client.ask_gemini(prompt, tier='tier2')

    if response and hasattr(response, 'text'):
        logger.info("EXECUTOR: Successfully refined task.")
        return response.text
    else:
        logger.warning("EXECUTOR: Failed to refine task. Falling back to the original.")
        return current_step_prompt