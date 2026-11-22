import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from patch_desktop_backend import patch_socket_first_uvicorn_startup

FIXTURE = """
import sys
import uvicorn
if __name__ == "__main__":
    _bind_host = "127.0.0.1"
    class Args:
        port = 8888
    _args = Args()
    app = object()
    kwargs = {"ssl_certfile": "server.crt", "ssl_keyfile": "server.key"}
    uvicorn.run(app,host=_bind_host,port=_args.port, **kwargs)
"""

class SocketFirstStartupTests(unittest.TestCase):
    def setUp(self):
        self.output = patch_socket_first_uvicorn_startup(FIXTURE)

    def test_bind_precedes_uvicorn_lifespan(self):
        self.assertLess(self.output.index(".bind(("), self.output.index("uvicorn.Server("))
        self.assertLess(self.output.index(".listen("), self.output.index("uvicorn.Server("))

    def test_uvicorn_receives_prebound_socket(self):
        self.assertIn("run(sockets=[_rdt_listen_socket])", self.output)
        self.assertNotIn("uvicorn.run(", self.output)

    def test_selected_port_and_tls_kwargs_are_preserved(self):
        self.assertIn("bind((_bind_host, _args.port))", self.output)
        self.assertIn("uvicorn.Config(app, host=_bind_host, port=_args.port, **kwargs)", self.output)

    def test_socket_is_closed_in_finally(self):
        self.assertIn("finally:", self.output)
        self.assertIn("_rdt_listen_socket.close()", self.output)

    def test_exclusive_binding_preferred_and_no_reuseport(self):
        self.assertIn("SO_EXCLUSIVEADDRUSE", self.output)
        self.assertIn("SO_REUSEADDR", self.output)
        self.assertNotIn("SO_REUSEPORT", self.output)

    def test_output_compiles_and_is_idempotent(self):
        compile(self.output, "<socket-first-test>", "exec")
        self.assertEqual(patch_socket_first_uvicorn_startup(self.output), self.output)

    def test_bind_collision_prevents_uvicorn_startup_and_closes_socket(self):
        import contextlib
        import io
        import socket
        import types
        from unittest.mock import patch

        occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(occupied.close)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        port = occupied.getsockname()[1]

        fixture = FIXTURE.replace("port = 8888", f"port = {port}")
        output = patch_socket_first_uvicorn_startup(fixture)
        calls = []
        fake_uvicorn = types.ModuleType("uvicorn")

        class Config:
            def __init__(self, *args, **kwargs):
                calls.append("config")

        class Server:
            def __init__(self, config):
                calls.append("server")

            def run(self, sockets):
                calls.append("run")

        fake_uvicorn.Config = Config
        fake_uvicorn.Server = Server
        namespace = {"__name__": "__main__"}
        stderr = io.StringIO()

        with patch.dict(sys.modules, {"uvicorn": fake_uvicorn}):
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(OSError):
                    exec(compile(output, "<busy-port-test>", "exec"), namespace, namespace)

        self.assertEqual(calls, [], "Uvicorn must not start when bind fails")
        self.assertEqual(namespace["_rdt_listen_socket"].fileno(), -1)
        self.assertIn("listener bind failed", stderr.getvalue())

    def test_ambiguous_launch_fails_closed(self):
        with self.assertRaises(SystemExit):
            patch_socket_first_uvicorn_startup(FIXTURE.replace(
                "uvicorn.run(app,host=_bind_host,port=_args.port, **kwargs)",
                "uvicorn.run(app,host=_bind_host,port=_args.port, **kwargs)\n    uvicorn.run(app,host=_bind_host,port=_args.port, **kwargs)"
            ))

if __name__ == "__main__":
    unittest.main(verbosity=2)
