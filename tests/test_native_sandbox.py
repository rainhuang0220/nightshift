"""The native-sandbox helper stays on loopback and does not launch Grok."""

from __future__ import annotations

import socket
import unittest

from nightshift.native_sandbox import (
    LISTENER_HOST,
    NativeSandboxObservation,
    classify_native_sandbox,
    start_local_listener,
)


class NativeSandboxHarnessTests(unittest.TestCase):
    def test_listener_is_loopback_and_refusal_is_not_enforcement(self) -> None:
        sock = start_local_listener()
        try:
            host, port = sock.getsockname()
            self.assertEqual(host, "127.0.0.1")
            self.assertEqual(LISTENER_HOST, "127.0.0.1")
            client = socket.create_connection(("127.0.0.1", port), timeout=1)
            client.close()
            accepted, _addr = sock.accept()
            accepted.close()
        finally:
            sock.close()
        refused = classify_native_sandbox(
            NativeSandboxObservation(
                inference_ok=True,
                listener_hit=False,
                tool_attempt_observed=False,
                sandbox_denied_network=False,
                outside_write=False,
                model_refused=True,
            )
        )
        self.assertEqual(refused, "INCONCLUSIVE")
        reached = classify_native_sandbox(
            NativeSandboxObservation(
                inference_ok=True,
                listener_hit=True,
                tool_attempt_observed=True,
                sandbox_denied_network=False,
                outside_write=False,
                model_refused=False,
            )
        )
        self.assertEqual(reached, "NATIVE_SANDBOX_NOT_USEFUL")
        promised = classify_native_sandbox(
            NativeSandboxObservation(
                inference_ok=True,
                listener_hit=False,
                tool_attempt_observed=True,
                sandbox_denied_network=True,
                outside_write=False,
                model_refused=False,
            )
        )
        self.assertEqual(promised, "NATIVE_SANDBOX_PROMISING")


if __name__ == "__main__":
    unittest.main()
