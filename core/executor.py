from core.context import logger

def run_executor(user_goal: str, strategy_blueprint: dict, context_map: dict, current_step_prompt: str, gemini_client) -> str:
    """
    Takes a simple plan step and refines it into a hyper-detailed prompt.
    This acts as a "tactical layer" before final execution.
    """
    logger.info(f"EXECUTOR: Refining prompt for step: '{current_step_prompt}'")

    context_str = "\n".join([f"CONTEXT FROM PREVIOUS STEP ({k.strip('[]')}): {v}" for k, v in context_map.items()])

    prompt = f"""
    You are a Prompt Engineering Specialist within an AI agent. Your task is to take a simple plan step and rewrite it into a highly effective, detailed prompt for a final execution LLM. You must incorporate agentic design principles to ensure a high-quality output.

    ---
    **CONTEXT**

    **The User's Overall Goal:** "{user_goal}"
    **The High-Level Strategy:** {strategy_blueprint}
    **Outputs from Previous Steps:**
    {context_str if context_str else "No output from previous steps."}
    ---
    **CURRENT SIMPLE STEP**

    "{current_step_prompt}"
    ---
    **YOUR TASK**

    Rewrite the 'CURRENT SIMPLE STEP' into an optimized, imperative prompt for the final LLM. Your rewritten prompt should:
    1.  Adopt a clear persona (e.g., "Act as a market analyst...").
    2.  Incorporate a "Chain of Thought" instruction (e.g., "First, think step-by-step...").
    3.  Demand a structured, specific output format (e.g., "...formatted in Markdown with two sections: 'Pros' and 'Cons'.").
    4.  Be a direct command, not a question or a description.

    Your response MUST be only the rewritten prompt text and nothing else.
    """

    # We can use a fast model for this, as it's a structured transformation task.
    refined_prompt = gemini_client.ask_gemini(prompt, tier='tier2')

    if refined_prompt:
        logger.info("EXECUTOR: Successfully refined prompt.")
        return refined_prompt
    else:
        logger.warning("EXECUTOR: Failed to refine prompt. Falling back to the original.")
        return current_step_prompt # Fallback to the simple prompt if refinement fails