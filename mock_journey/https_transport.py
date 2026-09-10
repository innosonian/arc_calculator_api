"""One bounded HTTPS exchange to an explicit, fixed destination.

This is not a CalculatorAdapter: it does not interpret or approve responses.
DNS resolution is synchronous and cannot be cancelled by socket timeouts. An
expired budget after DNS prevents connecting or sending, but cannot guarantee
the resolver itself returns within that budget. No retry occurs after sending.
"""

from dataclasses import dataclass, field
import http.client
import io
import math
import re
import socket
import ssl
import time
from urllib.parse import urlsplit


_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_OWNED_HEADERS = frozenset({
    "host", "content-length", "transfer-encoding", "connection", "upgrade",
    "expect", "trailer", "proxy-authorization", "proxy-connection",
})


class TransportError(Exception):
    """Fixed internal codes only; never retain request or remote error text."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code="INVALID_TRANSPORT_CONFIGURATION"):
    raise TransportError(code)


@dataclass(frozen=True)
class TransportLimits:
    connect_seconds: float
    io_seconds: float
    total_seconds: float
    max_request_bytes: int
    max_response_bytes: int

    def __post_init__(self):
        invalid_time = False
        try:
            invalid_time = any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                               for value in (self.connect_seconds, self.io_seconds, self.total_seconds))
        except OverflowError:
            invalid_time = True
        if invalid_time:
            _fail()
        if any(type(value) is not int or value <= 0
               for value in (self.max_request_bytes, self.max_response_bytes)):
            _fail()


@dataclass(frozen=True)
class HttpExchange:
    status: int
    headers: tuple[tuple[str, str], ...] = field(repr=False)
    body: bytes = field(repr=False)


class _Budget:
    def __init__(self, limits, clock):
        self.clock = clock
        started = clock()
        self.deadline = started + limits.total_seconds
        self.connect_deadline = min(self.deadline, started + limits.connect_seconds)
        self.io_seconds = limits.io_seconds

    def remaining(self, *, connecting=False):
        left = (self.connect_deadline if connecting else self.deadline) - self.clock()
        if left <= 0:
            _fail("TRANSPORT_TIMEOUT")
        return left if connecting else min(left, self.io_seconds)


def _close(resource):
    if resource is not None:
        try:
            resource.close()
        except (OSError, ValueError):
            pass


def _connect(host, port, budget, limits):
    # A socket timeout does not cover getaddrinfo. Check before AND after it.
    budget.remaining(connecting=True)
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    budget.remaining(connecting=True)
    context = ssl.create_default_context()
    # create_default_context can inherit SSLKEYLOGFILE diagnostics. Never
    # retain traffic-decryption secrets for an application data exchange.
    context.keylog_filename = None
    context.set_alpn_protocols(["http/1.1"])
    for family, kind, protocol, _, address in addresses:
        budget.remaining(connecting=True)
        plain = secure = None
        try:
            plain = socket.socket(family, kind, protocol)
            plain.settimeout(budget.remaining(connecting=True))
            try:
                plain.connect(address)
            except OSError:
                # Address fallback is allowed before any HTTP bytes exist.
                _close(plain)
                plain = None
                budget.remaining(connecting=True)
                continue
            plain.settimeout(budget.remaining(connecting=True))
            secure = context.wrap_socket(plain, server_hostname=host)
            budget.remaining(connecting=True)
            result, secure = secure, None
            return result
        finally:
            _close(secure)
            _close(plain)
    _fail("TRANSPORT_FAILED")


class _BudgetReader(io.RawIOBase):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        owner.readers += 1

    def readable(self):
        return True

    def readinto(self, buffer):
        if self.closed:
            raise ValueError("Reader is closed.")
        owner = self.owner
        owner.socket.settimeout(owner.budget.remaining())
        count = owner.socket.recv_into(buffer)
        owner.budget.remaining()
        return count

    def close(self):
        if not self.closed:
            self.owner.readers -= 1
            self.owner.release()
        super().close()


class _BudgetSocket:
    """Budget every recv, including HTTP parser header/chunk/trailer reads."""

    def __init__(self, secure, budget):
        self.socket, self.budget = secure, budget
        self.readers = 0
        self.close_requested = False

    def sendall(self, data):
        view = memoryview(data)
        while view:
            self.socket.settimeout(self.budget.remaining())
            sent = self.socket.send(view)
            self.budget.remaining()
            if sent <= 0:
                _fail("TRANSPORT_FAILED")
            view = view[sent:]

    def makefile(self, mode):
        if mode != "rb" or self.close_requested:
            _fail("TRANSPORT_FAILED")
        return io.BufferedReader(_BudgetReader(self))

    def release(self):
        # HTTPConnection closes an EOF-delimited connection before its response
        # body is read. Match socket.makefile's lifetime, retaining reader refs.
        if self.close_requested and not self.readers:
            _close(self.socket)

    def close(self):
        self.close_requested = True
        self.release()


def _destination(url):
    if (type(url) is not str or not url.startswith("https://") or "#" in url
            or "\\" in url or any(ord(char) <= 32 or ord(char) >= 127 for char in url)):
        _fail()
    try:
        parsed = urlsplit(url)
        if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.scheme != "https"):
            _fail()
        authority = parsed.netloc
        if authority.startswith("["):
            suffix = authority[authority.index("]") + 1:]
            if suffix and not re.fullmatch(r":[0-9]+", suffix):
                _fail()
        elif ":" in authority and not re.fullmatch(r"[^:]+:[0-9]+", authority):
            _fail()
        port = parsed.port
        if port is not None and port <= 0:
            _fail()
        host = parsed.hostname
        if any(char in host for char in "/%?#[]"):
            _fail()
        target = (parsed.path or "/") + (("?" + parsed.query) if "?" in url else "")
        return host, port or 443, target
    except (ValueError, UnicodeError):
        pass
    _fail()


def _headers(headers):
    if type(headers) is not tuple:
        _fail()
    seen = set()
    for pair in headers:
        if type(pair) is not tuple or len(pair) != 2:
            _fail()
        name, value = pair
        if (type(name) is not str or not _TOKEN.fullmatch(name) or type(value) is not str
                or any(ord(char) < 32 or ord(char) > 126 for char in value)):
            _fail()
        lowered = name.lower()
        if lowered in seen or lowered in _OWNED_HEADERS:
            _fail()
        seen.add(lowered)
    return headers


def _check_framing(response, headers, limit):
    lengths = [value for name, value in headers if name.lower() == "content-length"]
    encodings = [value for name, value in headers if name.lower() == "transfer-encoding"]
    if (len(lengths) > 1 or len(encodings) > 1 or (lengths and encodings)
            or (lengths and not re.fullmatch(r"[0-9]+", lengths[0].strip()))
            or (encodings and encodings[0].strip().lower() != "chunked")):
        _fail("INVALID_HTTP_RESPONSE")
    # A giant decimal may make stdlib int() raise and silently switch to EOF
    # framing. Compare canonical digits before conversion, then restore the
    # valid bounded length. HEAD/204/304 describe no actual response body.
    has_body = response._method != "HEAD" and response.status not in (204, 304) and not 100 <= response.status < 200
    if lengths and has_body:
        decimal = lengths[0].strip().lstrip("0") or "0"
        maximum = str(limit)
        if len(decimal) > len(maximum) or (len(decimal) == len(maximum) and decimal > maximum):
            _fail("RESPONSE_TOO_LARGE")
        response.length = int(decimal)
    if response.length is not None and response.length > limit:
        _fail("RESPONSE_TOO_LARGE")
    return response.length


class _StrictResponse(http.client.HTTPResponse):
    """Retain stdlib parsing with strict chunk sizes and terminators.

    Its permissive int(line, 16) accepts negative sizes (read(-1)), and it
    discards rather than validates chunk CRLFs. These overrides close those
    body-boundary gaps; line/count bounds are the existing stdlib constants.
    """

    def _line(self):
        line = self.fp.readline(http.client._MAXLINE + 1)
        if len(line) > http.client._MAXLINE or not line.endswith(b"\r\n"):
            _fail("INVALID_HTTP_RESPONSE")
        return line[:-2]

    def _read_next_chunk_size(self):
        value = self._line().split(b";", 1)[0]
        if not re.fullmatch(rb"[0-9A-Fa-f]+", value):
            _fail("INVALID_HTTP_RESPONSE")
        return int(value, 16)

    def _read_and_discard_trailer(self):
        for _ in range(http.client._MAXHEADERS + 1):
            if self._line() == b"":
                return
        _fail("INVALID_HTTP_RESPONSE")

    def _get_chunk_left(self):
        left = self.chunk_left
        if not left:
            if left is not None and self._safe_read(2) != b"\r\n":
                _fail("INVALID_HTTP_RESPONSE")
            left = self._read_next_chunk_size()
            if left == 0:
                self._read_and_discard_trailer()
                self._close_conn()
                left = None
            self.chunk_left = left
        return left


class HttpsTransport:
    def __init__(self, url, method, headers, limits, *, clock=time.monotonic):
        self._host, self._port, self._target = _destination(url)
        if (type(method) is not str or not _TOKEN.fullmatch(method)
                or method.upper() in {"CONNECT", "TRACE"} or type(limits) is not TransportLimits
                or not callable(clock)):
            _fail()
        self._method, self._headers = method, _headers(headers)
        self._limits, self._clock = limits, clock

    def exchange(self, body: bytes) -> HttpExchange:
        if type(body) is not bytes or len(body) > self._limits.max_request_bytes:
            _fail()
        budget = _Budget(self._limits, self._clock)
        wrapped = response = connection = None
        error_code = None
        try:
            secure = _connect(self._host, self._port, budget, self._limits)
            wrapped = _BudgetSocket(secure, budget)
            connection = http.client.HTTPConnection(self._host, self._port)
            # Override the stdlib class-level diagnostic flag explicitly.
            connection.debuglevel = 0
            connection.response_class = _StrictResponse
            # Only the already verified TLS socket may be used, even if the
            # HTTP parser closes it. Never implicitly reconnect in cleartext.
            connection.auto_open = 0
            connection.sock = wrapped
            connection.request(self._method, self._target, body=body, headers=dict(self._headers))
            response = connection.getresponse()
            budget.remaining()
            headers = tuple(response.getheaders())
            expected = _check_framing(response, headers, self._limits.max_response_bytes)
            chunks, received = [], 0
            while True:
                chunk = response.read(min(65536, self._limits.max_response_bytes + 1 - received))
                budget.remaining()
                received += len(chunk)
                if received > self._limits.max_response_bytes:
                    _fail("RESPONSE_TOO_LARGE")
                if not chunk:
                    break
                chunks.append(chunk)
            if expected is not None and received != expected:
                _fail("INVALID_HTTP_RESPONSE")
            return HttpExchange(response.status, headers, b"".join(chunks))
        except TransportError as error:
            error_code = error.code
        except TimeoutError:
            error_code = "TRANSPORT_TIMEOUT"
        except ssl.SSLError:
            error_code = "TRANSPORT_FAILED"
        except OverflowError:
            # A finite, positive timeout may exceed the platform socket range.
            # Do not invent an operating maximum or expose that raw exception.
            error_code = "INVALID_TRANSPORT_CONFIGURATION"
        except (http.client.HTTPException, ValueError, UnicodeError):
            error_code = "INVALID_HTTP_RESPONSE"
        except OSError:
            error_code = "TRANSPORT_FAILED"
        finally:
            _close(response)
            _close(connection)
            _close(wrapped)
        # Raise outside except: no retained exception context containing a URL,
        # request body, credentials, certificate identity or remote body text.
        raise TransportError(error_code)
