# Publishing auragrid-mcp 0.3.0 (owner steps; nothing here is automated)

Everything is built and tested; these steps need Joshua's accounts. All are free.

1. **PyPI** (needs a project-scoped API token for `auragrid-mcp`; the old tokens were revoked):

       cd ~/AurAgrid/auragrid_mcp
       rm -rf dist build && python -m pip install --upgrade build twine
       python -m build
       python -m twine upload dist/auragrid_mcp-0.3.0*      # username __token__, password = the token

   Check: `uvx auragrid-mcp --help` on any machine.

2. **Make the repo public** (the registry entry and awesome-lists link to it):
   GitHub -> auragridcorp-rgb/auragrid -> Settings -> Change visibility -> Public.
   History was purged and scanned clean on 2026-11-11.

3. **Official MCP Registry** (GitHub login as auragridcorp-rgb, device flow):

       # install mcp-publisher: https://github.com/modelcontextprotocol/registry/releases
       cd ~/AurAgrid/auragrid_mcp
       mcp-publisher login github          # open the printed URL, enter the code
       mcp-publisher publish               # reads server.json

   The README carries `mcp-name: io.github.auragridcorp-rgb/auragrid`, which the registry
   uses to confirm the PyPI package is ours. server.json validates against the
   2025-12-11 schema.

4. **PulseMCP** copies from the Official Registry automatically (its submissions are paused).
   **Glama** syncs with punkpeye/awesome-mcp-servers (see LISTINGS.md for the PR text).
   **Smithery** stopped accepting listings (registry now part of Arcade).
