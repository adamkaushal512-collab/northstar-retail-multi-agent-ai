"""Tools: read-only functions an AI alternative may request (Step 2.6B).

Each tool exposes a provider-shaped spec plus a plain Python function. The
model supplies only the tool's arguments; the access context is passed in
separately by trusted code [Decision D16]. Tools are read-only and perform
no business action [Decision D11, D18].

No tool execution loop, MCP or LangGraph is implemented here. Nothing here
selects an architecture or an agent count; alternative A can call the same
functions directly.
"""
