"""Offline client parsing, not host/transport cancellation certification."""

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
        self.assertEqual(body["config"]["configurable"]["thread_id"], "local-sse-client")
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
