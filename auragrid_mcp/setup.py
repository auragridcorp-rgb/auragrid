from setuptools import setup, find_packages

setup(
    name="auragrid-mcp",
    version="0.1.0",
    description="L402 Agent SDK for AurAgrid Telemetry",
    packages=find_packages(),
    install_requires=["requests"]
)
