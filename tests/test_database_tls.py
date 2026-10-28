"""Exercise database TLS startup with the installed PostgreSQL driver dependencies."""

from io import BytesIO
import ssl
from struct import pack
import unittest
from unittest.mock import MagicMock, patch

from tests import support
from tracker.configuration import CONFIG
from tracker.database import Store


class DatabaseTLSTests(unittest.TestCase):
    def test_store_opens_tls_connection_with_real_driver(self):
        """Catch incompatible driver dependencies in the production TLS startup path.

        Use the real pg8000 connection code and installed scramp package. Replace
        only AWS configuration and the transport, whose scripted PostgreSQL replies
        accept TLS and report authentication success. No database or AWS access is
        needed; this test does not verify a TLS handshake or password authentication.

        Args:
            self (DatabaseTLSTests): Test instance providing assertion helpers.

        Returns:
            None: Assert TLS startup succeeds, verification stays enabled, and the
            connection closes when leaving the store context.
        """
        tls = ssl.create_default_context()
        raw_socket = MagicMock()
        raw_socket.recv.return_value = b"S"
        tls_socket = MagicMock(spec=ssl.SSLSocket)
        # AuthenticationOk followed by ReadyForQuery, using PostgreSQL wire framing.
        replies = BytesIO(b"R" + pack("!II", 8, 0) + b"Z" + pack("!I", 5) + b"I")
        tls_socket.makefile.return_value.read.side_effect = replies.read
        settings = {
            "database_host": "database.example",
            "database_port": "5432",
            "database_name": "tracker_test",
        }

        with (
            patch.dict(CONFIG["database"], {"ssl_mode": "verify-full"}),
            patch("tracker.database.environment", side_effect=settings.get),
            patch("tracker.database.AWS_CLIENT.database_secret", return_value={
                "username": "test_user", "password": "test_password",
            }),
            patch("tracker.database.ssl.create_default_context", return_value=tls),
            patch("pg8000.core.socket.create_connection", return_value=raw_socket),
            patch.object(tls, "wrap_socket", return_value=tls_socket) as wrap,
        ):
            with Store.open() as store:
                self.assertTrue(store.connection.autocommit)
                wrap.assert_called_once_with(raw_socket, server_hostname="database.example")
                self.assertTrue(tls.check_hostname)
                self.assertEqual(tls.verify_mode, ssl.CERT_REQUIRED)
        tls_socket.close.assert_called_once()
