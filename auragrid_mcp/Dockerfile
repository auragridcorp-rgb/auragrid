# auragrid-mcp from PyPI (stdio MCP server). Paste this into Glama's Dockerfile field.
# Optional env: AURAGRID_URL (default https://84.12.79.124), payment cap settings: see README.md.
FROM python:3.12-slim
RUN pip install --no-cache-dir auragrid-mcp==0.3.0
ENTRYPOINT ["auragrid-mcp"]
