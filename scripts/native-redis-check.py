#!/usr/bin/env python3
"""Run production Lua contracts against a disposable local Redis process.

No TCP listener, production Redis connection or exchange access. Requires the
runtime and test requirements, and redis-server on PATH.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest


def main():
    binary = shutil.which("redis-server")
    if not binary:
        raise SystemExit("redis-server is required for the native contract check")
    # This process uses an isolated server; never inherit a live namespace.
    os.environ["CTS_G_NAME"] = "cts-g"
    os.environ["CTS_REDIS_PREFIX"] = ""
    import redis
    from test_redis_calculations import RedisCalculationTests
    from calculation_cache import CalculationCache
    from redis_coordination import RedisCoordinator, RedisUnavailable

    with tempfile.TemporaryDirectory(prefix="cts-redis-qa-") as directory:
        socket = str(Path(directory) / "redis.sock")
        with open(Path(directory) / "redis.log", "w+") as log:
            server = subprocess.Popen([binary, "--port", "0", "--unixsocket", socket,
                                       "--unixsocketperm", "700", "--save", "", "--appendonly", "no",
                                       "--maxmemory", "64mb", "--maxmemory-policy", "noeviction"],
                                      stdout=log, stderr=subprocess.STDOUT)
            client = redis.Redis(unix_socket_path=socket, decode_responses=True, socket_timeout=2)
            try:
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    try:
                        if client.ping():
                            break
                    except redis.ConnectionError:
                        if server.poll() is not None:
                            raise RuntimeError("Disposable Redis process exited during startup")
                        time.sleep(.05)
                else:
                    raise RuntimeError("Disposable Redis did not become ready")
                assert client.info("server")["process_id"] == server.pid
                assert client.config_get("port")["port"] == "0"

                class NativeContracts(RedisCalculationTests):
                    def setUp(self):
                        assert client.info("server")["process_id"] == server.pid
                        client.flushdb()  # This exact disposable child process only.
                        self.redis = client
                        self.cache = CalculationCache("bingx-x02", client=client)

                    def test_native_memory_guard_and_atomic_write(self):
                        config = RedisCoordinator(client)
                        self.assertTrue(config.write_hash("connection:bingx-x02", {"note": "test-only"}))
                        self.assertEqual(config.read_hash("connection:bingx-x02")["note"], "test-only")
                        self.assertGreater(client.memory_usage("connection:bingx-x02"), 0)
                        client.hset("connection:bingx-x02", "large", "x" * 2_000_000)
                        self.assertFalse(config.write_hash("connection:bingx-x02", {"mutated": "no"}))
                        self.assertFalse(client.hexists("connection:bingx-x02", "mutated"))
                        with self.assertRaises(RedisUnavailable):
                            config.read_hash("connection:bingx-x02", fresh=True)

                names = [name for name in unittest.defaultTestLoader.getTestCaseNames(NativeContracts)
                         if name not in {"test_configuration_hash_cache_coalesces_readers_and_write_invalidates",
                                         "test_existing_oversized_hash_rejects_atomically_and_does_not_cache_errors"}]
                print(f"Native Redis {client.info('server')['redis_version']} · isolated Unix socket · {len(names)} contracts", flush=True)
                result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(NativeContracts(name) for name in names))
                return 0 if result.wasSuccessful() else 1
            finally:
                client.close()
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
