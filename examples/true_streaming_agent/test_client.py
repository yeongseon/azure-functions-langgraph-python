"""Offline client parsing, not host/transport cancellation certification."""

from http.client import IncompleteRead
import io
import json
import unittest
from unittest.mock import MagicMock, patch

from client import StreamError, events, main


class ClientTests(unittest.TestCase):
    def test_fragmented_unicode_and_multiple_frames(self):
        wire = (
            ': heartbeat\r\nevent: data\r\ndata: {"text":"你好"}\r\n\r\n'
            "event: end\r\ndata: {}\r\n\r\n"
        ).encode()
        expected = [("data", {"text": "你好"})]
        self.assertEqual(list(events([wire])), expected)
        self.assertEqual(list(events([bytes([byte]) for byte in wire])), expected)

    def test_cr_only_end_at_eof(self):
        self.assertEqual(list(events([b"event: end\rdata: {}\r\r"])), [])

    def test_main_supplies_checkpoint_thread_id_and_closes(self):
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.status = 200
        response.getheader.return_value = "text/event-stream"
        response.read1.side_effect = [b"event: end\ndata: {}\n\n"]
        with (
            patch("client.HTTPConnection", return_value=connection),
            patch("sys.argv", ["client.py"]),
        ):
            main()
        body = json.loads(connection.request.call_args.kwargs["body"])
        self.assertTrue(body["config"]["configurable"]["thread_id"].startswith("local-sse-client-"))
        connection.close.assert_called_once()

    def run_main(self, *args, status=200, read1=None, body=b""):
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.status = status
        response.reason = "Conflict"
        response.read.return_value = body
        response.getheader.return_value = "text/event-stream"
        response.read1.side_effect = read1 or [b"event: end\ndata: {}\n\n"]
        stderr = io.StringIO()
        with (
            patch("client.HTTPConnection", return_value=connection),
            patch("sys.argv", ["client.py", *args]),
            patch("sys.stderr", stderr),
        ):
            try:
                main()
                code = 0
            except SystemExit as exit_:
                code = exit_.code
        return connection, code, stderr.getvalue()

    def test_each_run_gets_its_own_thread_id_unless_one_is_given(self):
        ids = []
        for args in ((), (), ("--thread-id", "mine")):
            connection, _, _ = self.run_main(*args)
            ids.append(
                json.loads(connection.request.call_args.kwargs["body"])["config"]["configurable"][
                    "thread_id"
                ]
            )
        self.assertNotEqual(ids[0], ids[1])
        self.assertEqual(ids[2], "mine")

    def test_http_error_reports_the_server_detail(self):
        detail = b'{"error":"error","detail":"Thread is currently in use by another request"}'
        _, code, stderr = self.run_main(status=409, body=detail)
        self.assertEqual(code, 1)
        self.assertIn("HTTP 409: Thread is currently in use by another request", stderr)

    def test_http_error_without_a_json_body_falls_back_to_the_reason(self):
        _, code, stderr = self.run_main(status=409, body=b"<html>")
        self.assertEqual(code, 1)
        self.assertIn("HTTP 409: Conflict", stderr)

    def test_abrupt_connection_close_is_a_clean_failure_not_a_traceback(self):
        connection, code, stderr = self.run_main(
            read1=[b"event: data\ndata: {}\n\n", IncompleteRead(b"")]
        )
        self.assertEqual(code, 1)
        self.assertIn("Stream failed:", stderr)
        self.assertNotIn("Traceback", stderr)
        connection.close.assert_called_once()

    def test_multibyte_frame_rejected_by_wire_size(self):
        wire = ('event: data\ndata: {"text":"' + "😀" * 300_000 + '"}\n\n').encode()
        self.assertLess(len(wire.decode()), 1024 * 1024)
        self.assertGreater(len(wire), 1024 * 1024)
        with self.assertRaisesRegex(StreamError, "exceeds"):
            list(events([wire]))

    def test_cr_frame_dispatched_before_next_read(self):
        def chunks():
            yield b"event: data\rdata: {}\r\r"
            raise AssertionError("Parser read again before delivering complete CR frame")

        stream = events(chunks())
        self.assertEqual(next(stream), ("data", {}))
        stream.close()

    def test_multiline_data_and_unknown_fields(self):
        wire = b'id: 1\nevent: data\ndata: {"value":\ndata: 2}\n\nevent: end\ndata: {}\n\n'
        self.assertEqual(list(events([wire])), [("data", {"value": 2})])

    def test_malformed_json(self):
        with self.assertRaisesRegex(StreamError, "Invalid JSON"):
            list(events([b"event: data\ndata: {broken}\n\n"]))

    def test_terminal_server_error(self):
        with self.assertRaisesRegex(StreamError, "Server emitted"):
            list(events([b'event: error\ndata: {"error":"failed"}\n\n']))

    def test_connection_loss_is_not_success(self):
        with self.assertRaisesRegex(StreamError, "before end"):
            list(events([b"event: data\ndata: {}\n\n"]))

    def test_client_disconnect_stops_consumption(self):
        consumed = []

        def chunks():
            consumed.append(1)
            yield b"event: data\ndata: {}\n\n"
            consumed.append(2)
            yield b"event: end\ndata: {}\n\n"

        stream = events(chunks())
        self.assertEqual(next(stream), ("data", {}))
        stream.close()
        self.assertEqual(consumed, [1])

    def test_oversized_frame(self):
        with self.assertRaisesRegex(StreamError, "exceeds"):
            list(events([b"x" * (1024 * 1024 + 1)]))


if __name__ == "__main__":
    unittest.main()
