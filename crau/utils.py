import codecs
import gzip
import re
import zlib
from urllib.parse import quote, urlparse, urlunparse

from warcio.archiveiterator import ArchiveIterator

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
    451: "Unavailable For Legal Reasons",
    460: "Client closed the connection",  # Unofficial/AWS Elastic Load Balancer
    463: "Too many forward IPs",  # Unofficial/AWS Elastic Load Balancer
    494: "Request header too large",  # Unofficial/nginx
    495: "SSL Certificate Error",  # Unofficial/nginx
    496: "SSL Certificate Required",  # Unofficial/nginx
    497: "HTTP Request Sent to HTTPS Port",  # Unofficial/nginx
    498: "Invalid Token",  # Unofficial/Esri
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
    526: "Invalid SSL Certificate",  # Unofficial/Cloudflare/Cloud Foundry
    527: "Railgun Error",  # Unofficial/Cloudflare
    530: "Site is frozen",  # Unofficial
    598: "Network read timeout error",  # Unofficial/Informal convention
}


def get_urls_from_file(filename, encoding="utf-8"):
    with open(filename, encoding=encoding) as fobj:
        for line in fobj:
            yield line.strip()


class WarcReader:
    def __init__(self, filename):
        self.filename = filename
        self.__fobj = None

    def __iter__(self):
        self.__fobj = open(self.filename, mode="rb")
        self.__iterator = ArchiveIterator(self.__fobj)
        return self

    def __next__(self):
        try:
            item = next(self.__iterator)
        except StopIteration:
            self.__fobj.close()
            self.__iterator = self.__fobj = None
            raise
        else:
            return item

    def get_response(self, uri):
        for record in self:
            if (
                record.rec_type == "response"
                and record.rec_headers.get_header("WARC-Target-URI") == uri
            ):
                return record

    def __del__(self):
        if self.__fobj is not None:
            self.__fobj.close()


def resource_matches_base_url(absolute_url, allowed):
    clean_allowed = []
    for allow in allowed:
        if not allow.startswith("http"):
            allow = f"http://{allow}"

        clean_allowed.append(urlparse(allow.replace("www.", "")))

    parsed_url = urlparse(absolute_url.replace("www.", ""))
    return (
        any(
            a.netloc == parsed_url.netloc and parsed_url.path.startswith(a.path)
            for a in clean_allowed
        )
        or not allowed
    )


def iri_to_uri(url: str) -> str:
    """Convert an IRI (non-ASCII/unsafe chars in path, query or host) to an ASCII-safe URI.

    Already percent-encoded sequences are preserved (idempotent), so calling it on
    a URL that came from the network is safe.
    """
    parsed = urlparse(url)
    netloc = parsed.netloc
    try:
        netloc.encode("ascii")
    except UnicodeEncodeError:
        host = parsed.hostname or ""
        netloc = host.encode("idna").decode("ascii")
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        if parsed.username:
            userinfo = quote(parsed.username, safe="")
            if parsed.password:
                userinfo += ":" + quote(parsed.password, safe="")
            netloc = f"{userinfo}@{netloc}"
    return urlunparse(
        (
            parsed.scheme,
            netloc,
            quote(parsed.path, safe="/%:@!$&'()*+,;=~"),
            quote(parsed.params, safe="/%:@!$&'()*+,;=~"),
            quote(parsed.query, safe="/%:@!$&'()*+,;=?~"),
            quote(parsed.fragment, safe="/%:@!$&'()*+,;=?~"),
        )
    )


REGEXP_CHARSET_PARAM = re.compile(rb"charset\s*=\s*[\"']?([A-Za-z0-9._:-]+)", re.IGNORECASE)
REGEXP_META_CHARSET = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?([A-Za-z0-9._:-]+)", re.IGNORECASE)
REGEXP_XML_ENCODING = re.compile(rb"^\s*<\?xml[^>]+encoding\s*=\s*[\"']([A-Za-z0-9._:-]+)", re.IGNORECASE)


def get_header(headers: list[tuple[bytes, bytes]], name: bytes) -> bytes | None:
    """Return the first value of a header (case-insensitive) or None."""
    name = name.lower()
    for key, value in headers:
        if key.lower() == name:
            return value
    return None


def decompress_body(body: bytes, headers: list[tuple[bytes, bytes]]) -> bytes:
    """Undo Content-Encoding (gzip, deflate) so the payload can be parsed; unknown encodings are returned as-is."""
    encoding = (get_header(headers, b"content-encoding") or b"").strip().lower()
    if not body or encoding in (b"", b"identity"):
        return body
    try:
        if encoding in (b"gzip", b"x-gzip"):
            return gzip.decompress(body)
        if encoding == b"deflate":
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
    except (OSError, EOFError, zlib.error):
        return body
    return body


def _valid_codec(name: bytes | str | None) -> str | None:
    if not name:
        return None
    if isinstance(name, bytes):
        name = name.decode("ascii", errors="replace")
    try:
        return codecs.lookup(name).name
    except LookupError:
        return None


def detect_charset(body: bytes, headers: list[tuple[bytes, bytes]]) -> str:
    """Charset from Content-Type, then BOM, then <meta charset>/<?xml encoding?>, defaulting to utf-8."""
    content_type = get_header(headers, b"content-type") or b""
    match = REGEXP_CHARSET_PARAM.search(content_type)
    codec = _valid_codec(match.group(1)) if match else None
    if codec:
        return codec

    if body.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if body.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"

    head = body[:4096]
    for regexp in (REGEXP_META_CHARSET, REGEXP_XML_ENCODING):
        match = regexp.search(head)
        codec = _valid_codec(match.group(1)) if match else None
        if codec:
            return codec
    return "utf-8"


def decode_body(body: bytes, headers: list[tuple[bytes, bytes]]) -> str:
    """Decompress and decode a response body to text using the declared charset (lossless fallback to latin-1)."""
    payload = decompress_body(body, headers)
    charset = detect_charset(payload, headers)
    try:
        return payload.decode(charset)
    except (UnicodeDecodeError, LookupError):
        pass
    if charset != "utf-8":
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return payload.decode("latin-1")
