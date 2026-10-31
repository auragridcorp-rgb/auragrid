from langchain.tools import BaseTool
from typing import Type, Optional
from pydantic import BaseModel, Field
from langchain_auragrid import AurAgridOracleTool

class AurAgridInput(BaseModel):
    endpoint_path: str = Field(description="The AurAgrid API endpoint path, e.g. energy/grid or batch/btc-fees")

class AurAgridCrewTool(BaseTool):
    name: str = "auragrid_telemetry_oracle"
    description: str = "Fetches AurAgrid data: Bitcoin fees and Lightning network stats (paid, L402) and free U.S. EIA grid mix, SPR, LNG exports, NOAA marine weather and USGS earthquakes. The live endpoint list is at /agent-manifest.json."
    args_schema: Type[BaseModel] = AurAgridInput

    def _run(self, endpoint_path: str) -> str:
        oracle = AurAgridOracleTool()
        return oracle.invoke({"endpoint_path": endpoint_path})
