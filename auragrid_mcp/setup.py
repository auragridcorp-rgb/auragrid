from setuptools import setup, find_packages

setup(
    name="auragrid-mcp",
    version="0.1.0",
    description="L402 client for the AurAgrid data oracle",
    long_description=("Python client for AurAgrid: Bitcoin fee and Lightning network data from AurAgrid's own "
                      "nodes (paid via L402) and free U.S. public-domain feeds (EIA grid mix, SPR, LNG exports; "
                      "NOAA marine weather; USGS earthquakes)."),
    long_description_content_type="text/plain",
    author="Joshua Sunge",
    author_email="auragridcorp@gmail.com",
    url="https://github.com/auragridcorp-rgb/auragrid",
    packages=find_packages(),
    install_requires=["requests"],
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: Other/Proprietary License",
        "Operating System :: OS Independent",
    ],
)
