from setuptools import find_packages, setup

setup(
    name="crau",
    description="Easy-to-use Web archiver",
    version="0.3.3",
    author="Álvaro Justen",
    author_email="alvarojusten@gmail.com",
    url="https://github.com/turicas/crau/",
    python_requires=">=3.10",
    install_requires=[
        "click",
        "h11",
        "lxml",
        "scrapy",
        "tqdm",
        "warcio",
    ],
    # pywb (only needed by `crau play`) does not support Python >= 3.13 and pins old
    # gevent/greenlet, so it is an extra instead of a hard dependency
    extras_require={
        "play": ["pywb"],
        "browser": ["websockets"],
        "lightpanda": ["websockets", "lightpanda"],
        "all": ["pywb", "websockets", "lightpanda"],
    },
    packages=find_packages(exclude=["*.tests", "*.tests.*", "tests.*", "tests"]),
    keywords="web crawling scraping archiving",
    entry_points={"console_scripts": ["crau = crau.cli:cli"]},
    classifiers=[
        "License :: OSI Approved :: GNU Lesser General Public License v3 or later (LGPLv3+)",
        "Natural Language :: English",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3.7",
    ],
)
