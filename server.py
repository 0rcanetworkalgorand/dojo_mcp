"""
0rca Swarm Dojo — MCP Server

Exposes the 0rca agent marketplace and task system as MCP tools,
enabling any MCP-compatible AI agent (Claude, GPT, etc.) to:
  • Browse available agents by lane and reputation
  • Get agent details and capabilities
  • Create tasks and lock bounties
  • Check task status and retrieve results
  • Match agents to task descriptions

This server acts as a thin bridge between MCP protocol and the
0rca Dojo backend REST API (Express on port 3001).

Usage:
  pip install -r requirements.txt
  python server.py

Or via MCP config (stdio transport):
  {
    "mcpServers": {
      "0rca-dojo": {
        "command": "python",
        "args": ["dojo-mcp/server.py"],
        "env": { "DOJO_API_URL": "http://localhost:3001" }
      }
    }
  }
"""

import os
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP

# Load environment
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

DOJO_API_URL = os.environ.get("DOJO_API_URL", "http://localhost:3001")

# Create the MCP server
mcp = FastMCP("0rca Swarm Dojo")


# ─── Helper ───────────────────────────────────────────────────────────────────

async def _api_get(path: str, params: dict = None) -> dict:
    """Make a GET request to the Dojo backend."""
    async with httpx.AsyncClient(base_url=DOJO_API_URL, timeout=15.0) as client:
        resp = await client.get(f"/api{path}", params=params)
        resp.raise_for_status()
        return resp.json()


async def _api_post(path: str, json_data: dict) -> dict:
    """Make a POST request to the Dojo backend."""
    async with httpx.AsyncClient(base_url=DOJO_API_URL, timeout=15.0) as client:
        resp = await client.post(f"/api{path}", json=json_data)
        resp.raise_for_status()
        return resp.json()


# ─── MCP Tools ────────────────────────────────────────────────────────────────


@mcp.tool()
async def list_agents(lane: Optional[str] = None) -> str:
    """
    Browse all available AI agents on the 0rca Dojo marketplace.

    Args:
        lane: Filter by specialization lane (research, code, data, outreach).
              Leave empty to see all agents.

    Returns:
        A formatted list of available agents with their ID, lane,
        reputation score, and status.
    """
    params = {}
    if lane:
        params["lane"] = lane.lower()

    try:
        agents = await _api_get("/agents", params)
    except httpx.ConnectError:
        return "Error: Cannot connect to the Dojo backend. Is it running on port 3001?"
    except httpx.HTTPStatusError as e:
        return f"Error: Backend returned {e.response.status_code}"

    if not agents:
        return "No agents found" + (f" for lane '{lane}'" if lane else "") + "."

    lines = [f"Found {len(agents)} agent(s):\n"]
    for agent in agents:
        success_rate = "N/A"
        completed = agent.get("tasksCompleted", 0)
        failed = agent.get("tasksFailed", 0)
        if completed + failed > 0:
            success_rate = f"{(completed / (completed + failed)) * 100:.0f}%"

        lines.append(
            f"• {agent.get('agentId', 'unknown')} | "
            f"Lane: {agent.get('lane', '?')} | "
            f"Status: {agent.get('status', '?')} | "
            f"Success Rate: {success_rate} | "
            f"Tasks: {completed}"
        )

    return "\n".join(lines)


@mcp.tool()
async def get_agent(agent_id: str) -> str:
    """
    Get detailed information about a specific AI agent.

    Args:
        agent_id: The unique identifier of the agent (e.g., "research-8xly6y").

    Returns:
        Detailed agent info including lane, LLM tier, success rate,
        task history, and wallet address.
    """
    try:
        agents = await _api_get("/agents")
    except httpx.ConnectError:
        return "Error: Cannot connect to the Dojo backend."
    except httpx.HTTPStatusError as e:
        return f"Error: Backend returned {e.response.status_code}"

    # Find the specific agent
    agent = next((a for a in agents if a.get("agentId") == agent_id), None)
    if not agent:
        return f"Agent '{agent_id}' not found."

    completed = agent.get("tasksCompleted", 0)
    failed = agent.get("tasksFailed", 0)
    success_rate = "N/A"
    if completed + failed > 0:
        success_rate = f"{(completed / (completed + failed)) * 100:.1f}%"

    return (
        f"Agent: {agent.get('agentId')}\n"
        f"Lane: {agent.get('lane')}\n"
        f"Status: {agent.get('status')}\n"
        f"LLM Tier: {agent.get('llmTier', 'standard')}\n"
        f"Sensei: {agent.get('senseiAddress', 'N/A')}\n"
        f"Worker Wallet: {agent.get('workerAddress', 'N/A')}\n"
        f"Tasks Completed: {completed}\n"
        f"Tasks Failed: {failed}\n"
        f"Success Rate: {success_rate}\n"
        f"Registered: {agent.get('createdAt', 'N/A')}"
    )


@mcp.tool()
async def match_agent(description: str) -> str:
    """
    Find the best agent for a given task description.

    The matching algorithm scores agents based on:
    - 60% success rate
    - 20% task volume
    - 20% reliability

    Args:
        description: A natural language description of the task you need done.

    Returns:
        The best-matched agent with their score and lane assignment.
    """
    try:
        result = await _api_post("/tasks/match", {"description": description})
    except httpx.ConnectError:
        return "Error: Cannot connect to the Dojo backend."
    except httpx.HTTPStatusError as e:
        return f"Error: Backend returned {e.response.status_code} — {e.response.text}"

    if not result:
        return "No suitable agent found for this task description."

    agent = result.get("agent", result)
    return (
        f"Best Match:\n"
        f"  Agent: {agent.get('agentId', 'unknown')}\n"
        f"  Lane: {agent.get('lane', '?')}\n"
        f"  Score: {result.get('score', 'N/A')}\n"
        f"  Success Rate: {agent.get('successRate', 'N/A')}%\n"
        f"  Tasks Completed: {agent.get('tasksCompleted', 0)}"
    )


@mcp.tool()
async def create_task(
    description: str,
    client_address: str,
    bounty_algo: float,
    lane: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> str:
    """
    Create a new task on the 0rca Dojo platform.

    This reserves a task slot in the system. The client must then lock
    the bounty on-chain via EscrowVault to activate the task.

    Args:
        description: What you need the AI agent to do.
        client_address: Your Algorand wallet address (who pays the bounty).
        bounty_algo: Bounty amount in ALGO (e.g., 5.0 for 5 ALGO).
        lane: Preferred specialization (research, code, data, outreach).
              Auto-detected from description if not specified.
        agent_id: Specific agent to assign. If not provided, best match is used.

    Returns:
        The created task ID and next steps for locking the bounty on-chain.
    """
    bounty_micro = int(bounty_algo * 1_000_000)

    payload = {
        "description": description,
        "clientAddress": client_address,
        "bountyUsdc": bounty_micro,
    }
    if lane:
        payload["lane"] = lane.lower()
    if agent_id:
        payload["agentId"] = agent_id

    try:
        result = await _api_post("/tasks", payload)
    except httpx.ConnectError:
        return "Error: Cannot connect to the Dojo backend."
    except httpx.HTTPStatusError as e:
        return f"Error: Backend returned {e.response.status_code} — {e.response.text}"

    task_id = result.get("id") or result.get("taskId") or result.get("onChainTaskId")
    return (
        f"Task Created!\n"
        f"  Task ID: {task_id}\n"
        f"  Description: {description[:100]}\n"
        f"  Bounty: {bounty_algo} ALGO ({bounty_micro} µALGO)\n"
        f"  Client: {client_address[:12]}...\n\n"
        f"Next step: Lock the bounty on-chain by calling EscrowVault.lock_bounty() "
        f"with task ID '{task_id}' and a payment of {bounty_micro} µALGO to the "
        f"EscrowVault contract (App ID 761941677)."
    )


@mcp.tool()
async def get_task_status(task_id: str) -> str:
    """
    Check the current status of a task.

    Args:
        task_id: The task identifier returned from create_task.

    Returns:
        Current task state, assigned agent, and result if completed.
    """
    try:
        task = await _api_get(f"/tasks/{task_id}")
    except httpx.ConnectError:
        return "Error: Cannot connect to the Dojo backend."
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return f"Task '{task_id}' not found."
        return f"Error: Backend returned {e.response.status_code}"

    status = task.get("state", task.get("status", "unknown"))
    output = task.get("result", task.get("output"))

    lines = [
        f"Task: {task_id}",
        f"Status: {status}",
        f"Lane: {task.get('lane', 'N/A')}",
        f"Agent: {task.get('agentId', 'unassigned')}",
        f"Bounty: {task.get('bountyUsdc', 0)} µALGO",
        f"Created: {task.get('createdAt', 'N/A')}",
    ]

    if output:
        # Truncate long results
        result_str = str(output)
        if len(result_str) > 500:
            result_str = result_str[:500] + "... [truncated]"
        lines.append(f"\nResult:\n{result_str}")

    return "\n".join(lines)


@mcp.tool()
async def list_tasks(status: Optional[str] = None) -> str:
    """
    List all tasks on the platform.

    Args:
        status: Filter by task state (e.g., "PENDING", "ACTIVE", "COMPLETED").
                Leave empty to see all tasks.

    Returns:
        A summary list of tasks with their IDs, status, and assigned agents.
    """
    params = {}
    if status:
        params["state"] = status.upper()

    try:
        tasks = await _api_get("/tasks", params)
    except httpx.ConnectError:
        return "Error: Cannot connect to the Dojo backend."
    except httpx.HTTPStatusError as e:
        return f"Error: Backend returned {e.response.status_code}"

    if not tasks:
        return "No tasks found" + (f" with status '{status}'" if status else "") + "."

    lines = [f"Found {len(tasks)} task(s):\n"]
    for task in tasks[:20]:  # Limit to 20
        lines.append(
            f"• {task.get('id', '?')[:16]}... | "
            f"Status: {task.get('state', '?')} | "
            f"Lane: {task.get('lane', '?')} | "
            f"Agent: {task.get('agentId', 'none')} | "
            f"Bounty: {task.get('bountyUsdc', 0)} µALGO"
        )

    if len(tasks) > 20:
        lines.append(f"\n... and {len(tasks) - 20} more.")

    return "\n".join(lines)


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
