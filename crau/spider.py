import logging
import re
from collections import namedtuple
from urllib.parse import urljoin

from scrapy import Request, Spider, signals
from scrapy.utils.request import request_fingerprint
from warcio.warcwriter import WARCWriter

from .utils import write_warc_request_response

Resource = namedtuple("Resource", ["name", "type", "content"])
REGEXP_CSS_URL = re.compile(r"""url\(['"]?(.*?)['"]?\)""")
# TODO: add all other "//link/@href"
# TODO: handle "//" URLs correctly
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


class CrauSpider(Spider):

    name = "crawler-spider"
    custom_settings = {
        "CONCURRENT_REQUESTS": 256,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 16,
        "DNSCACHE_ENABLED": True,
        "DNSCACHE_SIZE": 500000,
        "DNS_TIMEOUT": 5,
        "DOWNLOAD_MAXSIZE": 5 * 1024 * 1024,
        "DOWNLOAD_TIMEOUT": 5,
        "REACTOR_THREADPOOL_MAXSIZE": 40,
        "REDIRECT_ENABLED": False,
        "SCHEDULER_PRIORITY_QUEUE": "scrapy.pqueues.DownloaderAwarePriorityQueue",
        "SPIDER_MIDDLEWARES_BASE": {
            "scrapy.spidermiddlewares.httperror.HttpErrorMiddleware": 50,
            "scrapy.spidermiddlewares.offsite.OffsiteMiddleware": 500,
            "scrapy.spidermiddlewares.referer.RefererMiddleware": 700,
            "scrapy.spidermiddlewares.urllength.UrlLengthMiddleware": 800,
        },
    }

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        crawler.signals.connect(spider.spider_closed, signal=signals.spider_closed)
        return spider

    def __init__(self, warc_filename, urls, max_depth=1):
        super().__init__()
        self.max_depth = int(max_depth)
        self.warc_filename = warc_filename
        self.urls = urls
        self._request_history = set()
        self.warc_fobj = None
        self.warc_writer = None

    def spider_closed(self, spider):
        if self.warc_fobj is not None:
            self.warc_fobj.close()

    def make_request(self, request_class=Request, *args, **kwargs):
        """Method to create requests and implements a custom dedup filter"""

        kwargs["dont_filter"] = kwargs.get("dont_filter", True)
        kwargs["errback"] = kwargs.get("errback", self.parse_request_error)

        meta = kwargs.get("meta", {})
        meta["handle_httpstatus_all"] = meta.get("handle_httpstatus_all", True)
        meta["dont_redirect"] = meta.get("dont_redirect", True)
        kwargs["meta"] = meta

        request = request_class(*args, **kwargs)
        if "#" in request.url:
            request = request.replace(url=request.url[: request.url.find("#")])

        # This `if` filters duplicated requests - we don't use scrapy's dedup
        # filter because it has a bug, which filters out requests in undesired
        # cases <https://github.com/scrapy/scrapy/issues/1225>.
        # TODO: check if this dedup filter does not have the same problem
        # scrapy have (the problem is related to canonicalize request url).
        request_hash = request_fingerprint(request)
        # TODO: may move this in-memory set to a temp file since the number of
        # requests can be pretty large.
        if request_hash in self._request_history:
            return None
        else:
            self._request_history.add(request_hash)
            return request

    def write_warc(self, response):
        # TODO: transform this method into `write_response` so we can have
        # other response writers than WARC (CSV, for example - would be great
        # if we can add specific parsers to save HTML's title and text into
        # CSV, for example).
        write_warc_request_response(self.warc_writer, response)

    def start_requests(self):
        """Start requests with depth = 0

        depth will be 0 for all primary URLs and all requisites (CSS, Images
        and JS) of these URLs. For links found on these URLs, depth will be
        incremented, and so on.
        """
        self.warc_fobj = open(self.warc_filename, mode="wb")
        self.warc_writer = WARCWriter(self.warc_fobj, gzip=True)

        for url in self.urls:
            yield self.make_request(
                url=url, meta={"depth": 0, "main_url": url}, callback=self.parse
            )

    def parse(self, response):
        main_url = response.request.url
        # TODO: what if response.request.url != response.url?
        current_depth = response.request.meta["depth"]
        next_depth = current_depth + 1

        content_type = response.headers.get("Content-Type", b"").decode("ascii")
        if content_type and content_type.split(";")[0].lower() != "text/html":
            logging.debug(
                f"[{current_depth}] Content-Type not found for {main_url}, parsing as media"
            )
            yield self.parse_media(response)
            return

        logging.debug(f"[{current_depth}] Saving HTML {response.request.url}")
        self.write_warc(response)

        redirect_url = None
        if 300 <= response.status <= 399 and "Location" in response.headers:
            redirect_url = urljoin(
                response.request.url, response.headers["Location"].decode("ascii")
            )

        for resource in extract_resources(response):
            if resource.type == "link":
                for request in self.collect_link(
                    main_url,
                    resource.name,
                    urljoin(main_url, resource.content),
                    current_depth if resource.name != "other" else next_depth,
                ):
                    if (
                        request is None
                        or redirect_url is not None
                        and redirect_url == request.url
                    ):
                        continue
                    yield request

            elif resource.type == "code":
                for request in self.collect_code(
                    main_url, resource.name, resource.content, current_depth
                ):
                    if request is None:
                        continue
                    yield request

        if redirect_url is not None:
            # TODO: how to deal with redirect loops?
            logging.debug(f"[{current_depth}] Redirecting to {redirect_url}")
            yield self.make_request(
                url=redirect_url,
                meta={"depth": current_depth, "main_url": main_url},
                callback=self.parse,
            )

    def parse_request_error(self, failure):
        pass
        # TODO: should we do something with this failure?

    def parse_css(self, response):
        meta = response.request.meta

        for request in self.collect_code(
            response.request.url, "css", response.body, meta["depth"]
        ):
            if request is None:
                continue
            yield request

        logging.debug(f"Saving CSS {response.request.url}")
        self.write_warc(response)

    def parse_js(self, response):
        meta = response.request.meta

        for request in self.collect_code(
            response.request.url, "js", response.body, meta["depth"]
        ):
            if request is None:
                continue
            yield request

        logging.debug(f"Saving JS {response.request.url}")
        self.write_warc(response)

    def parse_media(self, response):
        logging.debug(f"Saving MEDIA {response.request.url}")
        self.write_warc(response)

    def collect_link(self, main_url, link_type, url, depth):
        if depth > self.max_depth:
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
                f"[{depth}] IGNORING (depth exceeded) getting dependencies for {code_type}"
            )
            return []
        elif code_type == "css":
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
