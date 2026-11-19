"""AurAgrid MCP server (in-repo entry point).

The server lives in the auragrid-mcp package (auragrid_mcp/auragrid_mcp/server.py):
tools list_feeds, get_sample, get_quote and fetch; fetch pays over L402 only within a
configured per-payment cap. Run it from this repository with:

    python auragrid_sdk/mcp_server.py           # stdio
    python auragrid_sdk/mcp_server.py --http 8765

or install the package: pip install auragrid-mcp  ->  auragrid-mcp
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "auragrid_mcp"))

from auragrid_mcp.server import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
