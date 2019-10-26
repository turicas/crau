import io
import logging
import re
from urllib.parse import urljoin, urlparse
from collections import namedtuple

import scrapy
from scrapy.utils.request import request_fingerprint
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter


# TODO: create a WARCWriter class
# TODO: create a extract_resources function


Resource = namedtuple("Resource", ["name", "type", "content"])
REGEXP_CSS_URL = re.compile(r"""url\(['"]?(.*?)['"]?\)""")
EXTRACTORS = {
    "media": {
        "link": (
            "//img/@src",
            "//audio/@src",
            "//video/@src",
            "//source/@src",
            "//embed/@src",
            "//object/@data",
        )
    },
    "css": {
        "link": ("//link[@rel = 'stylesheet']/@href",),
        "code": ("//style/text()", "//*/@style"),
    },
    "js": {
        "link": ("//script/@src",),
        "code": (
            "//script/text()",
            # TODO: add inline JS (onload, onchange, onclick etc.)
            # TODO: add "javascript:XXX" on //a/@href etc.
        ),
    },
    "other": {
        "link": (
            "//iframe/@src",
            "//a/@href",
            "//area/@href",
            "//link[not(@rel = 'stylesheet')]/@href",
        )
    },
}


def extract_resources(response):
    for resource_name, resource_types in EXTRACTORS.items():
        for resource_type, xpaths in resource_types.items():
            for xpath in xpaths:
                for content in response.xpath(xpath).extract():
                    yield Resource(
                        name=resource_name, type=resource_type, content=content
                    )


class CrauSpider(scrapy.Spider):

    name = "crawler-spider"
    # TODO: do not ignore 403 and other status codes (handle_httpstatus_list)
    # TODO: remove "#..." from URLs

    def __init__(self, warc_filename, urls, max_depth=1):
        super().__init__()
        self.max_depth = int(max_depth)
        self.warc_filename = warc_filename
        self.urls = urls
        self._request_history = set()

    def make_request(self, request_class=scrapy.Request, *args, **kwargs):
        """Method to create requests and implements a custom dedup filter"""

        if "dont_filter" not in kwargs:
            kwargs["dont_filter"] = True
        request = request_class(*args, **kwargs)
        request_hash = request_fingerprint(request)
        if request_hash in self._request_history:
            return None
        else:
            self._request_history.add(request_hash)
            return request

    def write_warc(self, response):
        request = response.request
        path = request.url[
            request.url.find("/", len(urlparse(request.url).scheme) + 3) :
        ]
        # TODO: fix if list has more than one value
        headers_list = [
            (key.decode("ascii"), value[0].decode("ascii"))
            for key, value in request.headers.items()
        ]
        # TODO: fix HTTP version
        http_headers = StatusAndHeaders(
            f"{request.method} {path} HTTP/1.1", headers_list, is_http_request=True
        )
        self.warc_writer.write_record(
            self.warc_writer.create_warc_record(
                request.url, "request", http_headers=http_headers
            )
        )

        # TODO: fix if list has more than one value
        headers_list = [
            (key.decode("ascii"), value[0].decode("ascii"))
            for key, value in response.headers.items()
        ]
        # TODO: fix status
        # TODO: fix HTTP version
        http_headers = StatusAndHeaders(
            f"{response.status} OK",
            headers_list,
            protocol="HTTP/1.1",
            is_http_request=False,
        )
        # TODO: what about redirects?
        self.warc_writer.write_record(
            self.warc_writer.create_warc_record(
                response.url,
                "response",
                payload=io.BytesIO(response.body),
                http_headers=http_headers,
            )
        )

    def start_requests(self):
        self.warc_fobj = open(self.warc_filename, mode="wb")
        self.warc_writer = WARCWriter(self.warc_fobj, gzip=True)
        # TODO: add self.warc_fobj.close() to spider finish

        for url in self.urls:
            yield self.make_request(
                url=url,
                meta={"depth": 0, "main_url": url},
                callback=self.parse,
                # TODO: check content type, then call specific parse method
            )

    def parse(self, response):
        main_url = response.request.url
        current_depth = response.request.meta["depth"]
        next_depth = current_depth + 1

        if (
            response.headers["Content-Type"].decode("ascii").split(";")[0].lower()
            != "text/html"
        ):
            yield self.parse_media(response)
            return

        logging.debug(f"Saving HTML {response.request.url}")
        self.write_warc(response)

        for resource in extract_resources(response):
            if resource.type == "link":
                for x in self.collect_link(
                    main_url,
                    resource.name,
                    urljoin(main_url, resource.content),
                    current_depth if resource.name != "other" else next_depth,
                ):
                    yield x
                    # TODO: refactor
            elif resource.type == "code":
                for x in self.collect_code(
                    main_url, resource.name, resource.content, current_depth
                ):
                    yield x
                    # TODO: refactor

    def parse_css(self, response):
        meta = response.request.meta

        for x in self.collect_code(
            response.request.url, "css", response.body, meta["depth"]
        ):
            yield x
            # TODO: refactor

        logging.debug(f"Saving CSS {response.request.url}")
        self.write_warc(response)

    def parse_js(self, response):
        meta = response.request.meta

        for x in self.collect_code(
            response.request.url, "js", response.body, meta["depth"]
        ):
            yield x
            # TODO: refactor

        logging.debug(f"Saving JS {response.request.url}")
        self.write_warc(response)

    def parse_media(self, response):
        meta = response.request.meta

        logging.debug(f"Saving MEDIA {response.request.url}")
        self.write_warc(response)

    def collect_link(self, main_url, link_type, url, depth):
        if depth >= self.max_depth:
            logging.debug(
                f"[{depth}] IGNORING (depth exceeded) get link {link_type} {url}"
            )
            return []
        elif not url.startswith("http"):
            logging.debug(f"[{depth}] IGNORING (not HTTP) get link {link_type} {url}")
            return []

        if link_type == "media":
            return [
                self.make_request(
                    url=url,
                    callback=self.parse_media,
                    meta={"depth": depth, "main_url": main_url},
                )
            ]
        elif link_type == "css":
            return [
                self.make_request(
                    url=url,
                    callback=self.parse_css,
                    meta={"depth": depth, "main_url": main_url},
                )
            ]
        elif link_type == "js":
            return [
                self.make_request(
                    url=url,
                    callback=self.parse_js,
                    meta={"depth": depth, "main_url": main_url},
                )
            ]
        elif link_type == "other":
            return [
                self.make_request(
                    url=url,
                    callback=self.parse,
                    meta={"depth": depth, "main_url": main_url},
                )
            ]
        else:
            return [
                self.make_request(
                    url=url,
                    callback=self.parse,
                    meta={"depth": depth, "main_url": main_url},
                )
            ]

    def collect_code(self, main_url, code_type, code, depth):
        if depth > self.max_depth:
            logging.debug(
                f"[{depth}] IGNORING (depth exceeded) get link {link_type} {url}"
            )
            return []

        if code_type == "css":
            if isinstance(code, bytes):
                code = code.decode("utf-8")  # TODO: decode properly
            requests = []
            for result in REGEXP_CSS_URL.findall(code):
                url = urljoin(main_url, result)
                if url.startswith("data:"):
                    continue
                requests.append(
                    self.make_request(
                        url=url,
                        callback=self.parse_media,
                        meta={"depth": depth, "main_url": main_url},
                    )
                )
            return requests
        elif code_type == "js":
            # TODO: extract other references from JS code
            return []
        else:
            logging.info(f"[{depth}] [TODO] PARSE CODE {code_type} {code}")
            return []
            # TODO: change
