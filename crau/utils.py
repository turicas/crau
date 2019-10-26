from scrapy.statscollectors import MemoryStatsCollector
from warcio.archiveiterator import ArchiveIterator
from tqdm import tqdm


def get_urls_from_file(filename, encoding="utf-8"):
    with open(filename, encoding=encoding) as fobj:
        for line in fobj:
            yield line.strip()


def get_warc_uris(filename):
    with open(filename, mode="rb") as fobj:
        for record in ArchiveIterator(fobj):
            if record.rec_type == "response":
                yield record.rec_headers.get_header("WARC-Target-URI")

def get_warc_record(filename, uri):
    with open(filename, mode="rb") as fobj:
        for record in ArchiveIterator(fobj):
            if record.rec_type == "response" and record.rec_headers.get_header("WARC-Target-URI") == uri:
                return record


class StdoutStatsCollector(MemoryStatsCollector):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.progress_bar = tqdm(desc="Downloading", unit="req",
                            unit_scale=True, dynamic_ncols=True
                            )

    def inc_value(self, key, count=1, start=0, spider=None):
        super().inc_value(key, count=count, start=start, spider=spider)
        if key == "response_received_count":
            self.progress_bar.n = self._stats["response_received_count"]
            self.progress_bar.refresh()
