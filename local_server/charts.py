"""Local, content-bound 300-second chart capabilities; no session authority."""

import base64
import hashlib
import hmac
import math
import re
import time
from urllib.parse import urlsplit

from mock_journey.errors import JourneyError
from local_server.object_storage import LocalObjectClient


_PREFIX = "/local/v1/charts/"
_TOKEN = re.compile(r"v1\.(0|[1-9][0-9]{0,10})\.(0|[1-9][0-9]{0,10})\.([0-9a-f]{64})\.([0-9a-f]{64})\.([A-Za-z0-9_-]{43})\Z")


class LocalChartService:
    path_prefix = _PREFIX

    def __init__(self, client, *, base_url, clock=time.time):
        try:
            from local_server.http import _address, _port

            if type(client) is not LocalObjectClient or type(base_url) is not str or not callable(clock):
                raise ValueError()
            parsed = urlsplit(base_url)
            host, port = _address(parsed.hostname), _port(parsed.port)
            if base_url != f"http://{host}:{port}" or parsed.username is not None or parsed.password is not None:
                raise ValueError()
            self.client, self.base_url, self.clock = client, base_url, clock
            self.artifact_limit = client.artifact_limit
            self._key = client.material.signing_key
            self._domain = ("arc-local-chart:v1\n" + client.material.installation_id + "\n"
                            + base_url + "\nGET\n" + _PREFIX + "\n").encode("ascii")
        except Exception:
            raise ValueError("Invalid local chart configuration.") from None

    def _now(self):
        try:
            value = self.clock()
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value < 99_999_999_000:
                raise ValueError()
            return int(value)
        except Exception:
            raise JourneyError("TEMPORARILY_UNAVAILABLE") from None

    def _signature(self, claim):
        raw = hmac.digest(self._key, self._domain + claim.encode("ascii"), "sha256")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def create_signed_url(self, key, *, expires_in=300):
        try:
            if type(expires_in) is not int or expires_in != 300:
                raise ValueError()
            ident, body = self.client.chart_object(key=key)
            now = self._now()
            claim = f"v1.{now}.{now + 300}.{ident}.{hashlib.sha256(body).hexdigest()}"
            return self.base_url + _PREFIX + claim + "." + self._signature(claim)
        except Exception:
            # This is an internal operation after authenticated publication.
            # No request value, underlying path or capability is echoed.
            raise JourneyError("TEMPORARILY_UNAVAILABLE") from None

    def read_path(self, path):
        now = self._now()
        try:
            if type(path) is not str or not path.startswith(_PREFIX):
                raise ValueError()
            token = path[len(_PREFIX):]
            match = _TOKEN.fullmatch(token)
            if not match:
                raise ValueError()
            issued, expires = int(match[1]), int(match[2])
            if expires - issued != 300 or not issued <= now < expires:
                raise ValueError()
            claim, signature = token.rsplit(".", 1)
            if not hmac.compare_digest(signature, self._signature(claim)):
                raise ValueError()
            ident, body = self.client.chart_object(ident=match[3])
            if ident != match[3] or not hmac.compare_digest(hashlib.sha256(body).hexdigest(), match[4]):
                raise ValueError()
            return body
        except Exception:
            raise JourneyError("NOT_FOUND") from None
