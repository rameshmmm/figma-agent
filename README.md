# Automated-UX-Designer-Agent

1. Download figma
2. Import plugin through manifest.json available in MCPServers/FigmaDesigner/src/mcp_plugin/manifest.json
3. Connect to port 3055 in the plugin popup.
4. Next, go to MCPServers/FigmaDesigner/src in your terminal and run `bun socket`
5. Finally, go to MCPClient and run `adk run figma_agent` once you've setup your .venv with all the packages of course.
