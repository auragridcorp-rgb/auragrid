from setuptools import setup, find_packages

setup(
    name="auragrid-mcp",
    version="0.3.0",
    description="MCP server and hardened L402 client for the AurAgrid data oracle",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    author="Joshua Sunge",
    author_email="auragridcorp@gmail.com",
    url="https://github.com/auragridcorp-rgb/auragrid",
    packages=find_packages(),
    install_requires=["requests>=2.28", "mcp>=1.2"],
    python_requires=">=3.10",
    entry_points={"console_scripts": ["auragrid-mcp=auragrid_mcp.server:main"]},
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
)
