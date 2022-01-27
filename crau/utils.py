import io
from urllib.parse import urlparse

from scrapy.statscollectors import MemoryStatsCollector
from tqdm import tqdm
from warcio.archiveiterator import ArchiveIterator
from warcio.statusandheaders import StatusAndHeaders

# Status/messages taken from <https://en.wikipedia.org/wiki/List_of_HTTP_status_codes>
HTTP_STATUS_CODES = {
    100: "Continue",
    101: "Switching Protocols",
    102: "Processing",
    103: "Early Hints",
    200: "OK",
    201: "Created",
    202: "Accepted",
    203: "Non-Authoritative Information",
    204: "No Content",
    205: "Reset Content",
    206: "Partial Content",
    207: "Multi-Status",
    208: "Already Reported",
    218: "This is fine",  # Unofficial/Apache Web Server
    226: "IM Used",
    300: "Multiple Choices",
    301: "Moved Permanently",
    302: "Found",
    303: "See Other",
    304: "Not Modified",
    305: "Use Proxy",
    306: "Switch Proxy",
    307: "Temporary Redirect",
    308: "Permanent Redirect",
    400: "Bad Request",
    401: "Unauthorized",
    402: "Payment Required",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    406: "Not Acceptable",
    407: "Proxy Authentication Required",
    408: "Request Timeout",
    409: "Conflict",
    410: "Gone",
    411: "Length Required",
    412: "Precondition Failed",
    413: "Payload Too Large",
    414: "URI Too Long",
    415: "Unsupported Media Type",
    416: "Range Not Satisfiable",
    417: "Expectation Failed",
    418: "I'm a teapot",
    419: "Page Expired",  # Unofficial/Laravel Framework
    420: "Enhance Your Calm",  # Unofficial/Twitter
    421: "Misdirected Request",
    422: "Unprocessable Entity",
    423: "Locked",
    424: "Failed Dependency",
    425: "Too Early",
    426: "Upgrade Required",
    428: "Precondition Required",
    429: "Too Many Requests",
    430: "Request Header Fields Too Large",  # Unofficial/Shopify
    431: "Request Header Fields Too Large",
    440: "Login Time-out",  # Unofficial/Internet Information Services
    444: "No Response",  # Unofficial/nginx
    449: "Retry With",  # Unofficial/Internet Information Services
    450: "Blocked by Windows Parental Controls",  # Unofficial/Microsoft
    451: "Redirect",  # Unofficial/Internet Information Services
    451: "Unavailable For Legal Reasons",
    460: "Client closed the connection",  # Unofficial/AWS Elastic Load Balancer
    463: "Too many forward IPs",  # Unofficial/AWS Elastic Load Balancer
    494: "Request header too large",  # Unofficial/nginx
    495: "SSL Certificate Error",  # Unofficial/nginx
    496: "SSL Certificate Required",  # Unofficial/nginx
    497: "HTTP Request Sent to HTTPS Port",  # Unofficial/nginx
    498: "Invalid Token",  # Unofficial/Esri
    499: "Client Closed Request",  # Unofficial/nginx
    499: "Token Required",  # Unofficial/Esri
    500: "Internal Server Error",
    501: "Not Implemented",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
    505: "HTTP Version Not Supported",
    506: "Variant Also Negotiates",
    507: "Insufficient Storage",
    508: "Loop Detected",
    509: "Bandwidth Limit Exceeded",  # Unofficial/Apache Web Server/cPanel
    510: "Not Extended",
    511: "Network Authentication Required",
    520: "Web Server Returned an Unknown Error",  # Unofficial/Cloudflare
    521: "Web Server Is Down",  # Unofficial/Cloudflare
    522: "Connection Timed Out",  # Unofficial/Cloudflare
    523: "Origin Is Unreachable",  # Unofficial/Cloudflare
    524: "A Timeout Occurred",  # Unofficial/Cloudflare
    525: "SSL Handshake Failed",  # Unofficial/Cloudflare
    526: "Invalid SSL Certificate",  # Unofficial/Cloudflare
    526: "Invalid SSL Certificate",  # Unofficial/Cloudflare/Cloud Foundry
    527: "Railgun Error",  # Unofficial/Cloudflare
    530: "Cloudflare Error",  # Unofficial/Cloudflare
    530: "Site is frozen",  # Unofficial
    598: "Network read timeout error",  # Unofficial/Informal convention
}


def get_urls_from_file(filename, encoding="utf-8"):
    with open(filename, encoding=encoding) as fobj:
        for line in fobj:
            yield line.strip()


def get_warc_uris(filename, record_type):
    with open(filename, mode="rb") as fobj:
        for record in ArchiveIterator(fobj):
            if record_type is None or record.rec_type == record_type:
                yield record.rec_headers.get_header("WARC-Target-URI")


def get_warc_record(filename, uri):
    with open(filename, mode="rb") as fobj:
        for record in ArchiveIterator(fobj):
            if (
                record.rec_type == "response"
                and record.rec_headers.get_header("WARC-Target-URI") == uri
            ):
                return record


class StdoutStatsCollector(MemoryStatsCollector):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.progress_bar = tqdm(
            desc="Downloading", unit="req", unit_scale=True, dynamic_ncols=True
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
    path = request.url[request.url.find("/", len(urlparse(request.url).scheme) + 3) :]

    http_headers = StatusAndHeaders(
        # TODO: fix HTTP version
        f"{request.method} {path} HTTP/1.1",
        get_headers_list(request.headers),
        is_http_request=True,
    )
    writer.write_record(
        writer.create_warc_record(request.url, "request", http_headers=http_headers)
    )

    # XXX: we're currently guessing the status "title" by its code, but this
    # title may not be the original from HTTP server.
    status_title = HTTP_STATUS_CODES.get(response.status, "Unknown")
    http_headers = StatusAndHeaders(
        f"{response.status} {status_title}",
        get_headers_list(response.headers),
        protocol="HTTP/1.1",
        # TODO: fix HTTP version
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
