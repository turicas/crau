from setuptools import find_packages, setup


setup(
    name="crau",
    description="Easy-to-use Web archiver",
    version="0.1.0",
    author="Álvaro Justen",
    author_email="alvarojusten@gmail.com",
    url="https://github.com/turicas/crau/",
    install_requires=[
        "click",
        "pywb",
        "scrapy",
        "tqdm",
        "warcio",
    ],
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
