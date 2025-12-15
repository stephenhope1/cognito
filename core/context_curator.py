import json
from typing import List, Dict, Any
from core.context import gemini_client, logger
from pydantic import BaseModel

class ContextCurator:
    """
    The 'Hydraulic' Context Engineer.
    It selectively pressurizes only the relevant information for the current task.
    """

    @staticmethod
    def get_relevant_context(current_task: str, completed_steps: List[Dict[str, Any]]) -> Dict[str, str]:
        """
        Analyzes the current task and the summaries of completed steps to determine
        which step outputs are actually needed.

        Args:
            current_task: The description or prompt of the step about to be executed.
            completed_steps: List of dicts containing 'step_id', 'output', and 'summary'.

        Returns:
            A context_map containing only the outputs of selected steps.
        """
        if not completed_steps:
            return {}

        # 1. Build the "Menu"
        menu_items = []
        for step in completed_steps:
            s_id = step.get('step_id')
            summary = step.get('summary') or (step.get('output', '')[:100] + "...")
            menu_items.append(f"Step ID {s_id}: {summary}")

        menu_str = "\n".join(menu_items)

        # 2. Ask the LLM to Select
        # We use a structured JSON output for reliability
        prompt = f"""
        **ROLE:** You are a Context Curator. Your job is to select strictly necessary information for the execution of a specific task.

        **CURRENT TASK:** "{current_task}"

        **AVAILABLE PREVIOUS OUTPUTS:**
        {menu_str}

        **INSTRUCTION:**
        Identify which of the above Step IDs contain information that is CRITICAL to complete the Current Task.
        - If the task depends on a previous result (e.g. "Analyze the code from Step 1"), select it.
        - If the task is independent, select nothing.
        - Be conservative. Do not include noise.

        **OUTPUT FORMAT:**
        Return ONLY a JSON object: {{ "selected_step_ids": [1, 3] }}
        """

        try:
            response = gemini_client.ask_gemini(
                prompt,
                tier='tier2',
                generation_config={"response_mime_type": "application/json", "temperature": 0.0}
            )

            selected_ids = []
            if response and hasattr(response, 'text'):
                try:
                    data = json.loads(response.text)
                    selected_ids = data.get("selected_step_ids", [])
                    # Handle if model returns a single int instead of list
                    if isinstance(selected_ids, int): selected_ids = [selected_ids]
                except json.JSONDecodeError:
                    logger.error("ContextCurator: Failed to parse JSON selection.")

            # 3. Hydrate the Context (Pressurize the selected lines)
            context_map = {}

            # Optimization: If list is empty but task explicitly mentions "Step X", fallback?
            # For now, trust the model.

            logger.info(f"ContextCurator: Task '{current_task[:50]}' requires steps: {selected_ids}")

            for step in completed_steps:
                if step['step_id'] in selected_ids:
                    # Key format must match what Executor expects
                    key = f"[output_of_step_{step['step_id']}]"
                    context_map[key] = step.get('output', '')

            return context_map

        except Exception as e:
            logger.error(f"ContextCurator Error: {e}")
            # Fallback: Return everything if curation fails?
            # Or return nothing? Safest might be last 1 step?
            # Let's return everything to be safe against breakage, but log error.
            logger.warning("ContextCurator: Falling back to full context due to error.")
            return {f"[output_of_step_{s['step_id']}]": s.get('output', '') for s in completed_steps}
