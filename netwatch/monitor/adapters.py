"""Connectivity check adapters.

Each adapter performs one check of one target and returns a ``CheckResult``.
ICMP is the primary adapter; DNS and HTTP are separate adapters registered in
``ADAPTERS`` so new check types can be added without touching the engine.
"""

from __future__ import annotations

import concurrent.futures
import http.client
import math
import os
import re
import shutil
import socket
import ssl
import time
from abc import ABC, abstractmethod
from urllib.parse import urlparse

from netwatch.models import CheckResult, ErrorCategory, Target
from netwatch.monitor.process import ProcessRunner


class CheckAdapter(ABC):
    check_type = ""
    label = ""

    @abstractmethod
    def check(self, target: Target, timeout: float) -> CheckResult: ...

    def availability(self) -> tuple[bool, str]:
        return True, "built in"

    def _result(self, target: Target, success: bool, started: float, **kw) -> CheckResult:
        return CheckResult(
            target_id=target.id,
            address=target.address,
            check_type=self.check_type,
            success=success,
            timestamp=started,
            packets_received=1 if success else 0,
            **kw,
        )


_TIME_RE = re.compile(r"time[=<]([\d.]+)\s*ms")


def classify_ping_failure(returncode: int, stdout: str, stderr: str) -> tuple[ErrorCategory, str]:
    text = f"{stdout}\n{stderr}".lower()
    if "operation not permitted" in text or "permission denied" in text or "cap_net_raw" in text:
        return ErrorCategory.PERMISSION, "ping is not permitted to open an ICMP socket"
    if "name or service not known" in text or "temporary failure in name resolution" in text or "unknown host" in text:
        return ErrorCategory.DNS, "could not resolve host name"
    if "network is unreachable" in text:
        return ErrorCategory.NETWORK_DOWN, "network is unreachable"
    if "unreachable" in text:
        return ErrorCategory.UNREACHABLE, "destination unreachable"
    if returncode == 1:
        return ErrorCategory.TIMEOUT, "no reply before timeout"
    line = (stderr.strip() or stdout.strip()).splitlines()
    return ErrorCategory.OTHER, (line[-1] if line else f"ping exited with status {returncode}")[:200]


class IcmpAdapter(CheckAdapter):
    check_type = "icmp"
    label = "ICMP ping"

    def __init__(self, runner: ProcessRunner, ping_path: str | None = None):
        self.runner = runner
        self.ping_path = ping_path or shutil.which("ping") or next(
            (p for p in ("/usr/bin/ping", "/bin/ping", "/usr/sbin/ping") if os.path.exists(p)), None
        )

    def availability(self) -> tuple[bool, str]:
        if not self.ping_path:
            return False, "The 'ping' command was not found. Install iputils (Fedora: iputils, Debian/Ubuntu: iputils-ping)."
        return True, self.ping_path

    def check(self, target: Target, timeout: float) -> CheckResult:
        started = time.time()
        if not self.ping_path:
            return self._result(target, False, started, error=ErrorCategory.MISSING_DEPENDENCY, detail="ping not installed")
        wait = max(1, int(math.ceil(timeout)))
        argv = [self.ping_path, "-n", "-c", "1", "-W", str(wait), target.address]
        mono = time.monotonic()
        try:
            res = self.runner.run(argv, timeout=wait + 3)
        except FileNotFoundError:
            return self._result(target, False, started, error=ErrorCategory.MISSING_DEPENDENCY, detail="ping not installed")
        except PermissionError:
            return self._result(target, False, started, error=ErrorCategory.PERMISSION, detail="cannot execute ping")
        elapsed_ms = (time.monotonic() - mono) * 1000
        if res.timed_out:
            return self._result(target, False, started, error=ErrorCategory.TIMEOUT, detail="ping process timed out")
        if res.returncode == 0:
            m = _TIME_RE.search(res.stdout)
            latency = float(m.group(1)) if m else elapsed_ms
            return self._result(target, True, started, latency_ms=latency)
        category, detail = classify_ping_failure(res.returncode, res.stdout, res.stderr)
        return self._result(target, False, started, error=category, detail=detail)


_resolver_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="nw-dns")


class DnsAdapter(CheckAdapter):
    """Resolve a host name with the system resolver."""

    check_type = "dns"
    label = "DNS lookup"

    def check(self, target: Target, timeout: float) -> CheckResult:
        started = time.time()
        mono = time.monotonic()
        future = _resolver_pool.submit(socket.getaddrinfo, target.address, None, 0, socket.SOCK_STREAM)
        try:
            future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            return self._result(target, False, started, error=ErrorCategory.TIMEOUT, detail="DNS lookup timed out")
        except socket.gaierror as exc:
            return self._result(target, False, started, error=ErrorCategory.DNS, detail=str(exc)[:200])
        except OSError as exc:
            return self._result(target, False, started, error=ErrorCategory.OTHER, detail=str(exc)[:200])
        return self._result(target, True, started, latency_ms=(time.monotonic() - mono) * 1000)


class HttpAdapter(CheckAdapter):
    """HEAD request; any HTTP response (even 4xx) proves reachability."""

    check_type = "http"
    label = "HTTP(S) request"

    def check(self, target: Target, timeout: float) -> CheckResult:
        started = time.time()
        url = target.address if "://" in target.address else f"https://{target.address}"
        parsed = urlparse(url)
        host = parsed.hostname or ""
        cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        kwargs = {"timeout": timeout}
        if parsed.scheme == "https":
            kwargs["context"] = ssl.create_default_context()
        mono = time.monotonic()
        conn = cls(host, parsed.port, **kwargs)
        try:
            conn.request("HEAD", parsed.path or "/", headers={"User-Agent": "NetWatch connectivity check"})
            resp = conn.getresponse()
            status = resp.status
        except socket.timeout:
            return self._result(target, False, started, error=ErrorCategory.TIMEOUT, detail="HTTP request timed out")
        except socket.gaierror as exc:
            return self._result(target, False, started, error=ErrorCategory.DNS, detail=str(exc)[:200])
        except (ConnectionRefusedError, ConnectionResetError) as exc:
            return self._result(target, False, started, error=ErrorCategory.UNREACHABLE, detail=str(exc)[:200])
        except OSError as exc:
            cat = ErrorCategory.NETWORK_DOWN if "unreachable" in str(exc).lower() else ErrorCategory.OTHER
            return self._result(target, False, started, error=cat, detail=str(exc)[:200])
        finally:
            conn.close()
        latency = (time.monotonic() - mono) * 1000
        if status >= 500:
            return self._result(target, False, started, error=ErrorCategory.HTTP_ERROR, detail=f"HTTP {status}", latency_ms=latency)
        return self._result(target, True, started, latency_ms=latency, detail=f"HTTP {status}")


def build_adapters(runner: ProcessRunner) -> dict[str, CheckAdapter]:
    return {"icmp": IcmpAdapter(runner), "dns": DnsAdapter(), "http": HttpAdapter()}
