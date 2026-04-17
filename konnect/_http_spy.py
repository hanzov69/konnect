"""Debug monkeypatch: log every request+response from the konnect SDK."""
import json, logging
from requests import Session

_log = logging.getLogger("konnect.http_spy")
_h = logging.FileHandler("/tmp/konnect_http.log")
_h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
_log.addHandler(_h); _log.setLevel(logging.DEBUG); _log.propagate = False
_orig = Session.request

def _spy(self, method, url, **kw):
    body = kw.get("json")
    hdrs = kw.get("headers") or {}
    safe_req_hdrs = {k: ("<REDACTED>" if k.lower() == "token" else v) for k, v in hdrs.items()}
    try:
        res = _orig(self, method, url, **kw)
        # IMPORTANT: also capture response headers — Connect delivers
        # commands as headers (Command-Id, Code) on telemetry responses.
        _log.info(
            "%s %s status=%s req_body=%s resp_headers=%s resp_body=%s",
            method, url, res.status_code,
            (json.dumps(body, default=str)[:200] if body else "-"),
            dict(res.headers),
            (res.text or "")[:200],
        )
        return res
    except Exception as e:
        _log.exception("%s %s EXC %s", method, url, e); raise

Session.request = _spy
_log.info("HTTP spy installed (v2: headers + body)")
