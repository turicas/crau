from setuptools import find_packages, setup

setup(
    name="crau",
    description="Easy-to-use, high-fidelity Web archiver and crawler",
    version="0.4.0",
    author="Álvaro Justen",
    author_email="alvarojusten@gmail.com",
    url="https://github.com/turicas/crau/",
    python_requires=">=3.10",
    # No hard dependency: browser automation runs on the standard library alone. Archiving and
    # crawling (archive, list, extract, pack and the Crawler API) need the `archive` extra.
    install_requires=[],
    # pywb (only needed by `crau play`) does not support Python >= 3.13 and pins old
    # gevent/greenlet, so it is an extra of its own
    extras_require={
        "archive": ["h11", "lxml", "tqdm", "warcio", "websockets"],
        "play": ["h11", "lxml", "tqdm", "warcio", "pywb"],
        "browser": ["h11", "lxml", "tqdm", "warcio", "websockets"],
        "lightpanda": ["h11", "lxml", "tqdm", "warcio", "websockets", "lightpanda"],
        "all": ["h11", "lxml", "tqdm", "warcio", "websockets", "pywb", "lightpanda"],
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
