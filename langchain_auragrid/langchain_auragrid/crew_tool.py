from langchain.tools import BaseTool
from typing import Type, Optional
from pydantic import BaseModel, Field
from langchain_auragrid import AurAgridOracleTool

class AurAgridInput(BaseModel):
    endpoint_path: str = Field(description="The AurAgrid API endpoint path, e.g. batch/ercot-lmp")

class AurAgridCrewTool(BaseTool):
    name: str = "auragrid_telemetry_oracle"
    description: str = "Fetches institutional-grade geospatial and energy telemetry from AurAgrid, automatically handling L402 Lightning micro-payments and cryptographic node verification."
    args_schema: Type[BaseModel] = AurAgridInput

    def _run(self, endpoint_path: str) -> str:
        oracle = AurAgridOracleTool()
        return oracle.invoke({"endpoint_path": endpoint_path})
