from setuptools import find_packages, setup

setup(
    name="crau",
    description="Easy-to-use, high-fidelity Web archiver and crawler",
    version="0.4.0",
    author="Álvaro Justen",
    author_email="alvarojusten@gmail.com",
    url="https://github.com/turicas/crau/",
    install_requires=[
        "h11",
        "lxml",
        "pywb",
        "tqdm",
        "warcio",
    ],
    extras_require={
        "browser": ["websockets"],
        "lightpanda": ["websockets", "lightpanda"],
        "all": ["websockets", "lightpanda"],
    },
    packages=find_packages(exclude=["*.tests", "*.tests.*", "tests.*", "tests"]),
    keywords="web crawling scraping archiving warc har",
    entry_points={"console_scripts": ["crau = crau.cli:cli"]},
    classifiers=[
        "License :: OSI Approved :: GNU Lesser General Public License v3 or later (LGPLv3+)",
        "Natural Language :: English",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3 :: Only",
    ],
)
