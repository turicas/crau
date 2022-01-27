# crau: Easy-to-use Web Archiver

*crau* is the way (most) Brazilians pronounce *crawl*, it's the easiest
command-line tool for archiving the Web and playing archives: you just need a
list of URLs.

## Installation

`pip install crau`


## Running

### Archiving

Archive a list of URLs by passing them via command-line:

```bash
crau archive myarchive.warc.gz http://example.com/page-1 http://example.org/page-2 ... http://example.net/page-N
```

or passing a text file (one URL per line):

```bash
echo "http://example.com/page-1" > urls.txt
echo "http://example.org/page-2" >> urls.txt
echo "http://example.net/page-N" >> urls.txt

crau archive myarchive.warc.gz -i urls.txt
```

Run `crau archive --help` for more options.

### Extracting data from an archive

List archived URLs in a WARC file:

```bash
crau list myarchive.warc.gz
```

Extract a file from an archive:

```bash
crau extract myarchive.warc.gz https://example.com/page.html extracted-page.html
```

### Playing the archived data on your Web browser

Run a server on [localhost:8080](http://localhost:8080) to play your archive:

```bash
crau play myarchive.warc.gz
```

## Why not X?

There are other archiving tools, of course. The motivation to start this
project was a lack of easy, fast and robust software to archive URLs - I just
wanted to execute one command without thinking and get a WARC file. Depending
on your problem, crau may not be the best answer - check out more archiving
tools in
[awesome-web-archiving](https://github.com/iipc/awesome-web-archiving#acquisition).

### Why not [GNU Wget](https://www.gnu.org/software/wget/)?

- Lacks parallel downloading;
- Some versions just crashes with segmentation fault depending on the website;
- Lots of options make the task of archiving difficult;
- There's no easy way to extend its behavior.

### Why not [Wpull](https://wpull.readthedocs.io/en/master/)?

- Lots of options make the task of archiving difficult;
- Easiest to extend than wget, but still difficult comparing to crau (since
  crau uses [scrapy](https://scrapy.org/)).

### Why not [crawl]()?

- Lacks some features and it's difficult to contribute to (the [Gitlab instance
  where it's hosted](https://git.autistici.org/ale/crawl) doesn't allow
  registration);
- Has some bugs regarding to collecting page dependencies (like static assets
  inside a CSS file);
- Has a bug where it enters in a loop (if a static asset returns a HTML page
  instead of the expected file it ignores depth and keep trying to get this
  page's dependencies - if any of the latter dependencies also has the same
  problem it keeps going on infinite depth).

### Why not [archivenow](https://github.com/oduwsdl/archivenow)?

This tool can be used easily to use archiving services such as
[archive.is](https://archive.is) via command-line and can also, but when
archiving it calls wget to do the job.


## Contributing

Clone the repository:

```bash
git clone https://github.com/turicas/crau.git
```

Install development dependencies (you may want to create a virtualenv):

```bash
cd crau && pip install -r requirements-development.txt
```

Install an editable version of the package:

```bash
pip install -e .
```

Modify everything you want to, commit to another branch and then create a pull
request at GitHub.
