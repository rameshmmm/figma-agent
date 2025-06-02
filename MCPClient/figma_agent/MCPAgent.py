# ./adk_agent_samples/mcp_agent/agent.py
from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset, StdioServerParameters
print("Yo")
root_agent = LlmAgent(
    model='gemini-2.5-flash-preview-05-20',
    name='figma_agent',
    instruction='You are an autonomous UX designer. You can perform CRUD operations on figma components. You can convert the user requirements into beautiful and standardized figma updations.',
    tools=[
        MCPToolset(
            connection_params=StdioServerParameters(
                command='bun',
                args=[
                    '/Users/rameshmanickam/Desktop/figmaMCPServer/cursor-talk-to-figma-mcp/src/talk_to_figma_mcp/server.ts'
                ],
            ),
        )
    ]
)
