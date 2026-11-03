"""Offline regressions for the proxy's upstream retry; no network is touched."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_vendor():
    spec = importlib.util.spec_from_file_location(
        "um_opencode_setup", ROOT / "vendor" / "um_opencode_setup.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup = load_vendor()


class FakeResponse:
    def __init__(self, status):
        self.status = status
        self.drained = False

    def read(self):
        self.drained = True
        return b"{}"


class FakeConnection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeHandler:
    """Stands in for ProxyHandler: only the pieces upstream_with_retry touches."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.connections = []

    def upstream(self, method, path, body=None):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        connection, response = FakeConnection(), FakeResponse(outcome)
        self.connections.append(connection)
        return connection, response


class UpstreamRetry(unittest.TestCase):
    def run_retry(self, outcomes, probe=None, renew=None):
        handler = FakeHandler(outcomes)
        probe = probe if probe is not None else (lambda token: None)
        renew = renew if renew is not None else (lambda: True)
        with patch.object(setup, "time") as clock, patch.object(
            setup, "probe_um_token", side_effect=probe
        ) as probe_mock, patch.object(
            setup, "renew_token_once", side_effect=renew
        ) as renew_mock, patch.object(
            setup, "current_token", return_value="token"
        ):
            result = setup.ProxyHandler.upstream_with_retry(handler, "GET", "/api/models")
        return handler, result, probe_mock, renew_mock, clock

    def test_a_healthy_response_is_not_retried(self):
        handler, (connection, response), probe, renew, clock = self.run_retry([200])
        self.assertEqual(response.status, 200)
        self.assertEqual(handler.calls, 1)
        probe.assert_not_called()
        renew.assert_not_called()
        clock.sleep.assert_not_called()

    def test_retries_a_transient_503(self):
        handler, (connection, response), probe, renew, clock = self.run_retry([503, 200])
        self.assertEqual(response.status, 200)
        self.assertEqual(handler.calls, 2)
        self.assertEqual(clock.sleep.call_count, 1)
        renew.assert_not_called()

    def test_failed_attempts_close_their_connection(self):
        handler, (connection, response), probe, renew, clock = self.run_retry([503, 200])
        self.assertTrue(handler.connections[0].closed)
        self.assertFalse(handler.connections[1].closed)

    def test_renews_only_when_the_probe_says_the_token_was_rejected(self):
        handler, (connection, response), probe, renew, clock = self.run_retry(
            [503, 200], probe=lambda token: False
        )
        self.assertEqual(response.status, 200)
        renew.assert_called_once()

    def test_an_outage_does_not_renew_the_token(self):
        handler, (connection, response), probe, renew, clock = self.run_retry(
            [503, 503, 503, 503], probe=lambda token: None
        )
        self.assertEqual(response.status, 503)
        self.assertEqual(handler.calls, 4)
        renew.assert_not_called()

    def test_the_probe_runs_at_most_once(self):
        handler, (connection, response), probe, renew, clock = self.run_retry(
            [503, 503, 503, 503], probe=lambda token: None
        )
        self.assertEqual(probe.call_count, 1)

    def test_connection_errors_are_retried_then_raised(self):
        with self.assertRaises(OSError):
            self.run_retry([OSError("boom")] * 4)

    def test_a_connection_error_can_recover(self):
        handler, (connection, response), probe, renew, clock = self.run_retry(
            [OSError("boom"), 200]
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(handler.calls, 2)

    def test_504_and_502_are_retried_too(self):
        handler, (connection, response), probe, renew, clock = self.run_retry([502, 504, 200])
        self.assertEqual(response.status, 200)
        self.assertEqual(handler.calls, 3)


if __name__ == "__main__":
    unittest.main()
