# crau: Easy-to-use Web Archiver


## Installation

`pip install crau`


## Running

Archive a list of URLs:

```bash
crau archive myarchive.warc.gz http://example.com/page-1 http://example.org/page-2 ... http://example.net/page-N
```

List archived URLs in a WARC file:

```bash
crau list myarchive.warc.gz
```

Extract a file from an archive:

```bash
crau extract myarchive.warc.gz https://example.com/page.html extracted-page.html
```

Run a server on [localhost:8080](http://localhost:8080) to play your archive:

```bash
crau play myarchive.warc.gz
```
