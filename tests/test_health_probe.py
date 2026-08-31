"""FR-5.1 sub-slice 2.1: the LLM health probe and its config plumbing.

`probe_llm_health` answers one question — is the model server answering
HTTP? — and must never raise. These tests pin the answer against:
- an in-process HTTP responder returning 200 (healthy), 404 (the server
  is alive even when the path is not routed — healthy) and 503
  (unhealthy);
- a closed port (connection refused — unhealthy);
- a responder that never answers in time (timeout — unhealthy).

The config plumbing (`Config.health_policy`) is disabled-safe: with no
`llmHealthUrl` the policy is disabled, and the FR-5.1 keys
(`llmHealthUrl`, `llmHealthEnabled`, `llmHealthTimeoutS`,
`llmHealthMaxAttempts`, `llmHealthBackoffBaseS`, `llmHealthBackoffCapS`)
fall back to the module defaults when absent.

No live model server, no container (NFR-4): the responders are
`http.server` on an ephemeral localhost port.

Run from the repo root:  python3 -m unittest tests.test_health_probe
"""
from __future__ import annotations

import http.server
import socket
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.core.config import Config, load as load_config
from harness.core.health import (
    DEFAULT_HEALTH_BACKOFF_BASE_S,
    DEFAULT_HEALTH_BACKOFF_CAP_S,
    DEFAULT_HEALTH_MAX_ATTEMPTS,
    DEFAULT_HEALTH_TIMEOUT_S,
    HealthPolicy,
    probe_llm_health,
)


class _Responder:
    """An in-process HTTP server answering every GET with `status`.

    `delay_s` makes the handler sleep before answering, past the probe's
    timeout, to exercise the timeout path.
    """

    def __init__(self, status: int = 200, delay_s: float = 0.0):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (http.server API)
                if outer.delay_s:
                    import time
                    time.sleep(outer.delay_s)
                self.send_response(status)
                self.end_headers()
                self.wfile.write(b"pong")

            def log_message(self, *args):
                pass  # keep the test output clean

            def handle(self):
                try:
                    super().handle()
                except BrokenPipeError:
                    pass  # the timeout probe hangs up before the answer

        self.delay_s = delay_s
        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        port = self.server.server_address[1]
        return f"http://127.0.0.1:{port}/health"

    def shutdown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _closed_port_url() -> str:
    """A port that was bound once and released — nothing listens there."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}/health"


def _policy(url: str, **overrides) -> HealthPolicy:
    fields = dict(url=url, enabled=True, timeout_s=2.0, max_attempts=1,
                  backoff_base_s=0.01, backoff_cap_s=0.01)
    fields.update(overrides)
    return HealthPolicy(**fields)


class ProbeTest(unittest.TestCase):
    def _probe(self, responder: _Responder, **overrides) -> bool:
        self.addCleanup(responder.shutdown)
        return probe_llm_health(_policy(responder.url, **overrides))

    def test_http_200_is_healthy(self):
        self.assertTrue(self._probe(_Responder(status=200)))

    def test_http_404_is_healthy_the_server_is_alive(self):
        """A routed-wrong path still proves the listener answers; the
        pre-flight asks "is the server up", not "is this path served"."""
        self.assertTrue(self._probe(_Responder(status=404)))

    def test_http_503_is_unhealthy(self):
        self.assertFalse(self._probe(_Responder(status=503)))

    def test_connection_refused_is_unhealthy_and_never_raises(self):
        self.assertFalse(probe_llm_health(_policy(_closed_port_url())))

    def test_timeout_is_unhealthy(self):
        responder = _Responder(status=200, delay_s=1.0)
        self.assertFalse(self._probe(responder, timeout_s=0.1))


class ConfigPlumbingTest(unittest.TestCase):
    """`Config.health_policy` — the FR-5.1 keys with disabled-safe defaults."""

    @staticmethod
    def _cfg(raw: dict) -> Config:
        return Config(
            work_dir=Path("/tmp/unused"),
            token_budget=100_000,
            max_spec_kickbacks=3,
            max_slice_implement=5,
            max_slice_tech_review=5,
            max_slice_func_review=5,
            max_slice_check_loops=3,
            autonomous_queue_target=5,
            trunk_branch="pi/trunk",
            task_provider="directory",
            directory_provider={},
            models={},
            model_context_map={},
            raw=raw,
        )

    def test_no_endpoint_configured_disables_the_gate(self):
        policy = self._cfg({}).health_policy()
        self.assertFalse(policy.enabled)
        self.assertEqual(policy.url, "")

    def test_url_present_enables_the_gate(self):
        policy = self._cfg({"llmHealthUrl": "http://h:8000/health"}).health_policy()
        self.assertTrue(policy.enabled)
        self.assertEqual(policy.url, "http://h:8000/health")

    def test_explicit_disable_wins_over_a_present_url(self):
        policy = self._cfg({"llmHealthUrl": "http://h:8000/health",
                            "llmHealthEnabled": False}).health_policy()
        self.assertFalse(policy.enabled)

    def test_absent_keys_fall_back_to_the_module_defaults(self):
        policy = self._cfg({"llmHealthUrl": "http://h:8000/health"}).health_policy()
        self.assertEqual(policy.timeout_s, DEFAULT_HEALTH_TIMEOUT_S)
        self.assertEqual(policy.max_attempts, DEFAULT_HEALTH_MAX_ATTEMPTS)
        self.assertEqual(policy.backoff_base_s, DEFAULT_HEALTH_BACKOFF_BASE_S)
        self.assertEqual(policy.backoff_cap_s, DEFAULT_HEALTH_BACKOFF_CAP_S)

    def test_configured_keys_override_the_defaults(self):
        policy = self._cfg({
            "llmHealthUrl": "http://h:8000/health",
            "llmHealthTimeoutS": 1.5,
            "llmHealthMaxAttempts": 7,
            "llmHealthBackoffBaseS": 0.25,
            "llmHealthBackoffCapS": 4.0,
        }).health_policy()
        self.assertEqual(policy.timeout_s, 1.5)
        self.assertEqual(policy.max_attempts, 7)
        self.assertEqual(policy.backoff_base_s, 0.25)
        self.assertEqual(policy.backoff_cap_s, 4.0)

    def test_shipped_config_json_stays_disabled(self):
        """The repo's own config.json configures no endpoint, so today's
        behavior is preserved for existing deployments (NFR-2)."""
        shipped = Path(__file__).resolve().parent.parent / "config.json"
        self.assertFalse(load_config(shipped).health_policy().enabled)


if __name__ == "__main__":
    unittest.main()
