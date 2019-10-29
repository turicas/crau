import io
from urllib.parse import urlparse

from scrapy.statscollectors import MemoryStatsCollector
from warcio.archiveiterator import ArchiveIterator
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter
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


def get_headers_list(headers):
    # TODO: fix if list has more than one value
    return [
        (key.decode("ascii"), value[0].decode("ascii"))
        for key, value in headers.items()
    ]

def write_warc_request_response(writer, response):
    request = response.request
    path = request.url[
        request.url.find("/", len(urlparse(request.url).scheme) + 3) :
    ]
    # TODO: fix HTTP version
    http_headers = StatusAndHeaders(
        f"{request.method} {path} HTTP/1.1",
        get_headers_list(request.headers),
        is_http_request=True
    )
    writer.write_record(
        writer.create_warc_record(
            request.url, "request", http_headers=http_headers
        )
    )

    # TODO: fix status
    # TODO: fix HTTP version
    http_headers = StatusAndHeaders(
        f"{response.status} OK",
        get_headers_list(response.headers),
        protocol="HTTP/1.1",
        is_http_request=False,
    )
    # TODO: what about redirects?
    writer.write_record(
        writer.create_warc_record(
            response.url,
            "response",
            payload=io.BytesIO(response.body),
            http_headers=http_headers,
        )
    )
