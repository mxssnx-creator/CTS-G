"""The gzip text heartbeat observed on the public BingX swap feed."""
import gzip
import json
import pathlib
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
import bingx_fast as bx


class HeartbeatTests(unittest.TestCase):
    def test_compressed_and_plain_heartbeats_keep_the_expected_protocol(self):
        ws = Mock()
        ws.recv.side_effect = [gzip.compress(b"Ping"), b"Ping", "Ping", "Pong",
                               gzip.compress(b"Pong"), '{"ping":123}', '{"ping":0}', None]
        hub = bx.PriceHub(Mock(), Mock())
        with patch.object(bx, "_ws", Mock(create_connection=Mock(return_value=ws))):
            hub._session()
        self.assertEqual([args.args[0] for args in ws.send.call_args_list], ["Pong", "Pong", "Pong", bx.dumps({"pong":123}), bx.dumps({"pong":0})])
        ws.close.assert_called_once()
        self.assertFalse(hub.ok)

    def test_disconnect_closes_socket_before_reconnect(self):
        ws = Mock()
        ws.recv.side_effect = OSError("connection lost")
        hub = bx.PriceHub(Mock(), Mock())
        with patch.object(bx, "_ws", Mock(create_connection=Mock(return_value=ws))):
            with self.assertRaises(OSError):
                hub._session()
        ws.close.assert_called_once()
        self.assertFalse(hub.ok)

    def test_quotes_and_malformed_frames_do_not_become_heartbeats(self):
        row = {"data": {"s": "BTC-USDT", "c": "50000"}}
        self.assertEqual(bx._decode_ws(gzip.compress(json.dumps(row).encode())), row)
        self.assertIsNone(bx._decode_ws(b""))
        self.assertIsNone(bx._decode_ws(b"invalid"))


if __name__ == "__main__":
    unittest.main()
