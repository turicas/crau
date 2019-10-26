# coding: utf-8

from setuptools import find_packages, setup

setup(
    name="crau",
    description=(
        "Web crawler",
    ),
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
    keywords="web crawling scraping",
    entry_points={"console_scripts": ["crau = crau.cli:cli"]},
    classifiers=[
        "Programming Language :: Python :: 3.6",
    ],
)
