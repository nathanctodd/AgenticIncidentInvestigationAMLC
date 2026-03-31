"""
Minimal Incident Investigation Agent (~50 lines)

This is the stripped-down version of main.py — showing the core agentic loop
with a single tool. Compare this to main.py to see what the full system adds:
multi-provider support, streaming UI, human approval, monitoring loop, etc.

Usage:
    ANTHROPIC_API_KEY=your_key python minimal_agent_example.py
"""

import anthropic
import subprocess

client = anthropic.Anthropic()

tools = [
    {
        "name": "search_logs",
        "description": "Search the application log file for a keyword or error pattern",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword or pattern to search for"}
            },
            "required": ["query"],
        },
    }
]


def run_tool(name: str, input: dict) -> str:
    if name == "search_logs":
        result = subprocess.run(
            ["grep", "-i", input["query"], "/app/logs/app.log"],
            capture_output=True,
            text=True,
        )
        output = result.stdout or result.stderr or "No results found."
        return output[-3000:]  # cap at 3000 chars to stay within context limits
    return f"Unknown tool: {name}"


def investigate(alert: str) -> str:
    messages = [{"role": "user", "content": alert}]

    for step in range(10):  # max 10 steps before giving up
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            tools=tools,
            system="You are an incident investigation agent. Use the search_logs tool to find the root cause of the issue. When you have enough information, produce a structured incident report with: root cause, evidence, and suggested next steps.",
            messages=messages,
        )

        # If the model is done reasoning, return its final response
        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text
            break

        # Otherwise, execute the tool calls and feed results back
        messages.append({"role": "assistant", "content": response.content})
        tool_results = [
            {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": run_tool(block.name, block.input),
            }
            for block in response.content
            if block.type == "tool_use"
        ]
        messages.append({"role": "user", "content": tool_results})

    return "Investigation incomplete — max steps reached."


if __name__ == "__main__":
    alert = "High error rate detected on the checkout service. Investigate the root cause."
    print(f"Alert: {alert}\n")
    print("--- Incident Report ---")
    print(investigate(alert))
