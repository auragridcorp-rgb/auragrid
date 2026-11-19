# Publishing auragrid-mcp 0.3.0 (owner steps; nothing here is automated)

Everything is built and tested; these steps need Joshua's accounts. All are free.

1. **PyPI** (needs a project-scoped API token for `auragrid-mcp`; the old tokens were revoked):

   Already built and checked (`twine check` PASSED, clean-venv install tested 2026-11-19):
   `dist/auragrid_mcp-0.3.0-py3-none-any.whl` and `dist/auragrid_mcp-0.3.0.tar.gz`.

       cd ~/AurAgrid/auragrid_mcp
       python3 -m pip install --user twine
       python3 -m twine upload dist/auragrid_mcp-0.3.0*     # username __token__, password = the token

   Check: `uvx auragrid-mcp --help` on any machine.

2. **Make the repo public** (the registry entry and awesome-lists link to it):
   GitHub -> auragridcorp-rgb/auragrid -> Settings -> Change visibility -> Public.
   History was purged on 2026-11-11 and re-scanned clean on 2026-11-19 (all 51 commits: no
   macaroons, private keys, nsec, API keys, tokens or the dashboard password).

3. **Official MCP Registry** (GitHub login as auragridcorp-rgb, device flow):

       # mcp-publisher v1.8.9 is already installed in ~/.local/bin (checksum-verified 2026-11-19;
       # `mcp-publisher validate` passes). To reinstall:
       cd /tmp && B=https://github.com/modelcontextprotocol/registry/releases/download/v1.8.9
       curl -sLO $B/mcp-publisher_linux_arm64.tar.gz && curl -sLO $B/registry_1.8.9_checksums.txt
       grep 'mcp-publisher_linux_arm64.tar.gz$' registry_1.8.9_checksums.txt | sha256sum -c -
       tar xzf mcp-publisher_linux_arm64.tar.gz mcp-publisher && install -m 755 mcp-publisher ~/.local/bin/
       cd ~/AurAgrid/auragrid_mcp
       mcp-publisher login github          # open the printed URL, enter the code
       mcp-publisher publish               # reads server.json

   The README carries `mcp-name: io.github.auragridcorp-rgb/auragrid`, which the registry
   uses to confirm the PyPI package is ours. server.json validates against the
   2025-12-11 schema.

4. **PulseMCP** copies from the Official Registry automatically (its submissions are paused).
   **Glama** syncs with punkpeye/awesome-mcp-servers (see LISTINGS.md for the PR text).
   **Smithery** stopped accepting listings (registry now part of Arcade).
