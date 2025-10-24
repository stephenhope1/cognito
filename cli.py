import click
import uuid
from core.context import logger
from utils.database import add_goal, get_active_goals, initialize_database
from core.planner import orchestrate_planning

@click.group()
def cli():
    """Cognito AI Agent Command-Line Interface."""
    pass

@click.command()
@click.argument('goal_text', type=str)
def add(goal_text: str):
    """Adds a new high-level goal by orchestrating the Strategist -> Planner pipeline."""
    click.echo(f"CLI: Received new goal: '{goal_text}'")

    # MODIFIED: All old planning logic is replaced with this single call
    new_goal_obj = orchestrate_planning(goal_text)

    if new_goal_obj:
        # Add the final unique ID and source before saving
        new_goal_obj['goal_id'] = f"cli_{'clarification' if new_goal_obj['status'] == 'awaiting_input' else 'goal'}_{uuid.uuid4()}"
        add_goal(new_goal_obj)
        click.echo(f"✅ Successfully created and added new goal '{new_goal_obj['goal_id']}' to the database.")
        if new_goal_obj['status'] == 'awaiting_input':
            click.echo(f"Agent needs more info: {new_goal_obj['plan'][0]['tool_call']['parameters']['question']}")
            click.echo("Please provide input via the dashboard to continue this goal.")
    else:
        click.echo("❌ CLI ERROR: Failed to create a plan for the goal. Aborting.")


@click.command()
def status():
    """Displays the status of all active goals from the database."""
    goals = get_active_goals()
    
    if not goals:
        click.echo("No active goals in the queue.")
        return

    click.echo("\n--- Cognito Agent Status ---")
    for goal in goals:
        status_text = goal['status'].upper()
        
        if goal['status'] == 'complete': color = 'green'
        elif goal['status'] == 'in-progress': color = 'yellow'
        elif goal['status'] == 'failed': color = 'red'
        else: color = 'white'
        
        click.echo(click.style(f"\n[{status_text}] Goal: {goal['goal']}", fg=color, bold=True))
        if 'plan' in goal and goal['plan']:
            completed_steps = sum(1 for step in goal['plan'] if step['status'] == 'complete')
            total_steps = len(goal['plan'])
            click.echo(f"Progress: {completed_steps}/{total_steps} steps complete.")
    click.echo("\n--------------------------")

if __name__ == '__main__':
    initialize_database()
    cli()