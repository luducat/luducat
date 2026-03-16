# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# bridge_client.py

"""Playnite Bridge Client

TLS client for communicating with the luducat bridge plugin running inside
Playnite. Handles:
- Connection management (TLS 1.3 over TCP)
- Pairing protocol (ECDSA P-256 key exchange, HKDF verification code, signature challenge)
- Session management (HMAC-TOTP silent reconnect)
- Request/response correlation via nonces
- RFC 1918 address validation

Wire protocol: newline-delimited JSON over TLS.
See design/playnite-bridge-protocol.md for full specification.
"""

import hashlib
import hmac
import ipaddress
import logging
import secrets
import socket
import ssl
import struct
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Protocol constants
PROTOCOL_VERSION = "1.1.0"
MIN_PROTOCOL_VERSION = "1.0.0"
DEFAULT_PORT = 39817
MAX_MESSAGE_SIZE = 65536  # 64 KB
KEEPALIVE_INTERVAL = 30  # seconds
KEEPALIVE_TIMEOUT = 90  # 3 missed pings
CONNECT_TIMEOUT = 5  # seconds
REQUEST_TIMEOUT = 10  # seconds
PAIRING_TIMEOUT = 120  # 2 minutes

# HKDF salts (must match C# bridge)
VERIFY_SALT = b"luducat-bridge-verify-v1"
VERIFY_INFO = b"verification-code"
TOTP_SALT = b"luducat-bridge-totp-v1"
TOTP_INFO = b"totp-secret"

_kx = hmac.new(TOTP_SALT, VERIFY_SALT, hashlib.sha256).digest()

# RFC 1918 private networks
_PRIVATE_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fe80::/10"),
]

# Credential file for stored pairing state
_CRED_FILENAME = "bridge_pairing.json"


def is_private_address(addr: str) -> bool:
    """Check if an IP address is in RFC 1918 private ranges or loopback."""
    try:
        ip = ipaddress.ip_address(addr)
        return any(ip in net for net in _PRIVATE_NETWORKS)
    except ValueError:
        return False


def _derive_verification_code(
    our_pubkey: bytes, their_pubkey: bytes,
    server_cert_der: Optional[bytes] = None,
    ts_window: int = 0,
) -> str:
    """Derive 6-digit verification code from both public keys via HKDF."""
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives import hashes

    sorted_keys = sorted([our_pubkey, their_pubkey])
    ikm = sorted_keys[0] + sorted_keys[1]

    if server_cert_der:
        cert_hash = hashlib.sha256(server_cert_der).digest()
        ikm = ikm + cert_hash

    if ts_window:
        ikm = ikm + ts_window.to_bytes(8, "big")

    ikm = hmac.new(_kx, ikm, hashlib.sha256).digest()

    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=4,
        salt=VERIFY_SALT,
        info=VERIFY_INFO,
    )
    code_bytes = hkdf.derive(ikm)
    code = int.from_bytes(code_bytes, "big") % 1000000
    return f"{code:06d}"


def _derive_totp_secret(
    our_pubkey: bytes, their_pubkey: bytes
) -> bytes:
    """Derive 20-byte TOTP shared secret from both public keys via HKDF."""
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives import hashes

    sorted_keys = sorted([our_pubkey, their_pubkey])
    ikm = sorted_keys[0] + sorted_keys[1]

    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=20,
        salt=TOTP_SALT,
        info=TOTP_INFO,
    )
    return hkdf.derive(ikm)


def _compute_totp(secret: bytes, time_step: int = 30, window: int = 0) -> str:
    """Compute a 6-digit TOTP value (RFC 6238).

    Args:
        secret: 20-byte shared secret
        time_step: Time period in seconds (default 30)
        window: Time window offset (-1, 0, +1)

    Returns:
        6-digit TOTP string
    """
    counter = int(time.time()) // time_step + window
    counter_bytes = struct.pack(">Q", counter)
    h = hmac.new(secret, counter_bytes, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code = struct.unpack(">I", h[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{code % 1000000:06d}"


def _verify_totp(secret: bytes, totp_value: str, time_step: int = 30) -> bool:
    """Verify a TOTP value with ±1 window tolerance."""
    for window in (-1, 0, 1):
        if _compute_totp(secret, time_step, window) == totp_value:
            return True
    return False


class BridgeClient:
    """Client for the Playnite bridge IPC protocol.

    Manages the TLS connection, handles pairing and authentication,
    and provides a request/response interface for bridge commands.
    """

    def __init__(self, host: str, port: int, data_dir: Path):
        self._host = host
        self._port = port
        self._data_dir = data_dir
        self._sock: Optional[ssl.SSLSocket] = None
        self._buffer = b""
        self._last_error: Optional[str] = None  # Last error for UI reporting
        self._last_activity: float = 0.0  # monotonic timestamp of last send/recv

        # Pairing state (loaded from credential store)
        self._our_private_key = None
        self._our_public_key = None
        self._peer_public_key = None
        self._totp_secret: Optional[bytes] = None
        self._permissions: list = []
        self._server_cert_fingerprint: Optional[str] = None  # SHA-256 hex
        self._ts_window: int = 0

        self._load_credentials()

    # ── Public API ────────────────────────────────────────────────────

    @staticmethod
    def has_stored_credentials(data_dir: Path) -> bool:
        """Check if pairing credentials exist on disk."""
        cred_path = data_dir / _CRED_FILENAME
        return cred_path.is_file()

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    def is_connected(self) -> bool:
        """Check if the TLS connection is alive."""
        if self._sock is None:
            return False
        # Probe the underlying socket for obvious disconnects
        try:
            self._sock.getpeername()
        except OSError:
            self._close_socket()
            return False
        return True

    def ensure_alive(self) -> bool:
        """Check connection liveness via keepalive ping. Reconnect if stale.

        Called before sending requests on persistent sessions. If the
        connection has been idle longer than KEEPALIVE_INTERVAL, sends a
        ping to verify the TCP flow is still alive. If the ping fails
        (NAT conntrack drop, VM network loss), closes the dead socket
        and attempts a fresh connect+auth.

        Returns True if connected (possibly after reconnect).
        """
        if self._sock is None:
            return False
        elapsed = time.monotonic() - self._last_activity
        if elapsed <= KEEPALIVE_INTERVAL:
            return True
        # Idle too long — probe with a ping
        try:
            self._sock.settimeout(CONNECT_TIMEOUT)
            nonce = secrets.token_hex(8)
            self._send_message({"type": "ping", "nonce": nonce})
            resp = self._recv_response(nonce, CONNECT_TIMEOUT)
            if resp and resp.get("type") == "pong":
                self._last_activity = time.monotonic()
                return True
        except Exception:
            pass
        # Dead — reconnect
        logger.info("Bridge keepalive failed after %.0fs idle, reconnecting", elapsed)
        self._close_socket()
        return self.connect()

    def connect(self) -> bool:
        """Connect and authenticate to the bridge.

        If paired, uses HMAC-TOTP for silent reconnection.
        Returns True on success.
        """
        if not is_private_address(self._host):
            logger.error(
                "Bridge host %s is not a private address — refusing connection",
                self._host,
            )
            self._last_error = "not a private address"
            return False

        if self._totp_secret is None:
            logger.error("Not paired — cannot connect to bridge")
            self._last_error = "not paired"
            return False

        try:
            self._establish_tls()
            if not self._authenticate():
                return False

            # Re-pin certificate if it changed (bridge restart generates
            # new self-signed cert). TOTP auth proves it's the same bridge.
            if self._cert_fp_changed and self._sock is not None:
                peer_cert_der = self._sock.getpeercert(binary_form=True)
                if peer_cert_der:
                    new_fp = hashlib.sha256(peer_cert_der).hexdigest()
                    logger.info(
                        "Re-pinning bridge certificate after TOTP auth: %s",
                        new_fp,
                    )
                    self._server_cert_fingerprint = new_fp
                    self._save_credentials()
                self._cert_fp_changed = False

            return True
        except Exception as e:
            logger.error("Bridge connection failed: %s", e)
            self._last_error = str(e)
            self._close_socket()
            return False

    def disconnect(self) -> None:
        """Graceful disconnect (preserves pairing)."""
        if self._sock is not None:
            try:
                self._send_message({
                    "type": "disconnect",
                    "nonce": secrets.token_hex(8),
                    "reason": "shutdown",
                })
            except Exception:
                pass
            self._close_socket()

    def send_request(
        self, message: Dict[str, Any], timeout: float = REQUEST_TIMEOUT
    ) -> Optional[Dict[str, Any]]:
        """Send a request and wait for the matching response.

        Args:
            message: JSON-serializable dict with 'type' and 'nonce'
            timeout: Maximum wait time in seconds

        Returns:
            Response dict or None on timeout/error
        """
        if not self.is_connected():
            return None

        nonce = message.get("nonce")
        if not nonce:
            nonce = secrets.token_hex(8)
            message["nonce"] = nonce

        try:
            self._send_message(message)
            return self._recv_response(nonce, timeout)
        except Exception as e:
            logger.error("Bridge request failed: %s", e)
            self._close_socket()
            return None

    # ── Pairing ───────────────────────────────────────────────────────

    def start_pairing(self) -> Optional[str]:
        """Initiate pairing with the bridge.

        Returns:
            6-digit verification code to display, or None on failure.
        """
        from cryptography.hazmat.primitives.asymmetric.ec import (
            ECDSA, SECP256R1, generate_private_key,
        )
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            PublicFormat,
            NoEncryption,
            PrivateFormat,
        )

        if not is_private_address(self._host):
            logger.error("Bridge host %s is not a private address", self._host)
            return None

        # Generate our ECDSA P-256 keypair
        self._our_private_key = generate_private_key(SECP256R1())
        self._our_public_key = self._our_private_key.public_key().public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint
        )  # 65 bytes: 0x04 || X || Y

        try:
            # Connect with TLS (no pin check — first contact)
            self._establish_tls(pin_check=False)

            import base64
            from luducat.core.constants import APP_VERSION

            ts_window = int(time.time()) // 300

            # Step 1: Send pair_hello
            nonce = secrets.token_hex(8)
            self._send_message({
                "type": "pair_hello",
                "nonce": nonce,
                "version": PROTOCOL_VERSION,
                "min_version": MIN_PROTOCOL_VERSION,
                "client_version": APP_VERSION,
                "public_key": base64.b64encode(self._our_public_key).decode(),
                "timestamp": ts_window,
            })

            # Step 2: Receive pair_hello_reply
            reply = self._recv_response(nonce, PAIRING_TIMEOUT)
            if reply is None or reply.get("status") != "ok":
                error_msg = (
                    reply.get("error_message", "Pairing rejected")
                    if reply else "No response from bridge"
                )
                logger.error("Pairing hello failed: %s", reply)
                self._last_error = error_msg
                self._close_socket()
                return None

            peer_key_b64 = reply.get("public_key", "")
            self._peer_public_key = base64.b64decode(peer_key_b64)

            # Step 3: Derive verification code
            server_cert_der = self._sock.getpeercert(binary_form=True)
            self._ts_window = ts_window
            code = _derive_verification_code(
                self._our_public_key, self._peer_public_key,
                server_cert_der=server_cert_der,
                ts_window=ts_window,
            )

            return code

        except Exception as e:
            logger.error("Pairing initiation failed: %s", e)
            self._last_error = str(e)
            self._close_socket()
            return None

    def complete_pairing(
        self, verification_code: str, permissions: list
    ) -> bool:
        """Complete pairing after user confirms verification code.

        Includes ECDSA P-256 signature challenge to prevent MITM attacks.

        Args:
            verification_code: The 6-digit code displayed to the user
            permissions: List of permission strings (e.g., ["launch"])

        Returns:
            True if pairing completed successfully
        """
        from cryptography.hazmat.primitives.asymmetric.ec import ECDSA
        from cryptography.hazmat.primitives.asymmetric.utils import (
            decode_dss_signature,
        )
        from cryptography.hazmat.primitives import hashes

        if self._sock is None or self._peer_public_key is None:
            return False

        try:
            import base64

            # Step 4: Send pair_verify
            nonce = secrets.token_hex(8)
            self._send_message({
                "type": "pair_verify",
                "nonce": nonce,
                "verification_code": verification_code,
            })

            reply = self._recv_response(nonce, PAIRING_TIMEOUT)
            if reply is None or reply.get("status") != "ok":
                logger.error("Pairing verification failed: %s", reply)
                return False

            # ── Signature challenge (MITM prevention) ──────────────
            # Step 5a: Send challenge to bridge
            challenge_bytes = secrets.token_bytes(32)
            nonce = secrets.token_hex(8)
            self._send_message({
                "type": "pair_challenge",
                "nonce": nonce,
                "challenge": challenge_bytes.hex(),
            })

            reply = self._recv_response(nonce, PAIRING_TIMEOUT)
            if reply is None or reply.get("status") != "ok":
                logger.error("Challenge exchange failed: %s", reply)
                return False

            # Step 5b: Verify bridge's signature
            bridge_sig_b64 = reply.get("bridge_signature", "")
            bridge_challenge_hex = reply.get("bridge_challenge", "")
            bridge_sig = base64.b64decode(bridge_sig_b64)
            bridge_challenge = bytes.fromhex(bridge_challenge_hex)

            # Bridge signed: our_challenge || our_public_key
            verify_payload = challenge_bytes + self._our_public_key
            if not self._verify_peer_signature(
                self._peer_public_key, verify_payload, bridge_sig
            ):
                logger.error(
                    "Bridge signature verification failed — possible MITM"
                )
                self._close_socket()
                return False

            # Step 5c: Sign bridge's challenge and send response
            # We sign: bridge_challenge || bridge_public_key
            sign_payload = bridge_challenge + self._peer_public_key
            our_sig_der = self._our_private_key.sign(
                sign_payload, ECDSA(hashes.SHA256())
            )

            # Convert DER signature to IEEE P1363 format (raw r||s)
            # C# CNG expects P1363, Python cryptography produces DER
            r, s = decode_dss_signature(our_sig_der)
            our_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")

            nonce = secrets.token_hex(8)
            self._send_message({
                "type": "pair_challenge_response",
                "nonce": nonce,
                "client_signature": base64.b64encode(our_sig).decode(),
            })

            reply = self._recv_response(nonce, PAIRING_TIMEOUT)
            if reply is None or reply.get("status") != "ok":
                logger.error(
                    "Challenge response rejected: %s",
                    reply.get("error_message") if reply else "timeout",
                )
                self._close_socket()
                return False

            # ── Permissions ────────────────────────────────────────
            # Step 6: Send permissions
            nonce = secrets.token_hex(8)
            self._send_message({
                "type": "pair_permissions",
                "nonce": nonce,
                "permissions": permissions,
            })

            reply = self._recv_response(nonce, PAIRING_TIMEOUT)
            if reply is None or reply.get("status") != "ok":
                logger.error("Permissions rejected: %s", reply)
                return False

            self._permissions = reply.get("granted_permissions", permissions)

            # Step 7: Finalize
            nonce = secrets.token_hex(8)
            self._send_message({
                "type": "pair_complete",
                "nonce": nonce,
            })

            reply = self._recv_response(nonce, PAIRING_TIMEOUT)
            if reply is None or reply.get("status") != "ok":
                logger.error("Pairing completion failed: %s", reply)
                return False

            # Derive TOTP secret
            self._totp_secret = _derive_totp_secret(
                self._our_public_key, self._peer_public_key
            )

            # Pin the server certificate
            peer_cert_der = self._sock.getpeercert(binary_form=True)
            if peer_cert_der:
                self._server_cert_fingerprint = hashlib.sha256(
                    peer_cert_der
                ).hexdigest()

            self._save_credentials()

            # Close the pairing session
            self._close_socket()

            logger.info("Pairing with Playnite bridge completed successfully")
            return True

        except Exception as e:
            logger.error("Pairing completion failed: %s", e)
            self._close_socket()
            return False

    @staticmethod
    def _verify_peer_signature(
        peer_pubkey_bytes: bytes, data: bytes, signature: bytes
    ) -> bool:
        """Verify an ECDSA P-256 signature from the peer's public key.

        Accepts both DER-encoded and IEEE P1363 (raw r||s) formats.
        C# CNG produces P1363 (64 bytes for P-256).
        """
        from cryptography.hazmat.primitives.asymmetric.ec import (
            ECDSA, EllipticCurvePublicKey, SECP256R1,
        )
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric.utils import (
            encode_dss_signature,
        )

        try:
            # C# CNG produces IEEE P1363 format (raw r||s, 64 bytes)
            # Python cryptography expects DER-encoded signatures
            if len(signature) == 64:
                r = int.from_bytes(signature[:32], "big")
                s = int.from_bytes(signature[32:], "big")
                signature = encode_dss_signature(r, s)

            peer_key = EllipticCurvePublicKey.from_encoded_point(
                SECP256R1(), peer_pubkey_bytes
            )
            peer_key.verify(signature, data, ECDSA(hashes.SHA256()))
            return True
        except Exception:
            return False

    def unpair(self) -> None:
        """Sever the pairing relationship and wipe credentials."""
        if self.is_connected():
            try:
                self._send_message({
                    "type": "unpair",
                    "nonce": secrets.token_hex(8),
                })
            except Exception:
                pass
            self._close_socket()

        self._our_private_key = None
        self._our_public_key = None
        self._peer_public_key = None
        self._totp_secret = None
        self._permissions = []
        self._delete_credentials()

    # ── Transport ─────────────────────────────────────────────────────

    def _establish_tls(self, pin_check: bool = True) -> None:
        """Create a TLS connection to the bridge.

        Args:
            pin_check: If True and a pinned cert fingerprint exists,
                       verify the server cert matches. Set to False
                       during initial pairing (no fingerprint yet).
        """
        raw_sock = socket.create_connection(
            (self._host, self._port), timeout=CONNECT_TIMEOUT
        )

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # Self-signed cert, trust via pin

        self._sock = ctx.wrap_socket(raw_sock, server_hostname=self._host)
        self._buffer = b""

        # Check certificate fingerprint on reconnect.
        # A mismatch is NOT fatal — the bridge regenerates its self-signed
        # cert on restart. HMAC-TOTP is the real trust anchor. If auth
        # succeeds despite a cert change, we update the pinned fingerprint.
        self._cert_fp_changed = False
        if pin_check and self._server_cert_fingerprint:
            peer_cert_der = self._sock.getpeercert(binary_form=True)
            if peer_cert_der is None:
                logger.error("Bridge did not present a certificate")
                self._close_socket()
                raise ConnectionError("No server certificate")

            actual_fp = hashlib.sha256(peer_cert_der).hexdigest()
            if actual_fp != self._server_cert_fingerprint:
                logger.warning(
                    "Server certificate fingerprint changed — "
                    "expected %s, got %s. Will re-pin if TOTP auth succeeds.",
                    self._server_cert_fingerprint, actual_fp,
                )
                self._cert_fp_changed = True

    def _authenticate(self) -> bool:
        """Authenticate via HMAC-TOTP after TLS handshake."""
        if self._totp_secret is None or self._peer_public_key is None:
            return False

        totp = _compute_totp(self._totp_secret)
        fingerprint = hashlib.sha256(self._peer_public_key).hexdigest()

        nonce = secrets.token_hex(8)
        self._send_message({
            "type": "auth",
            "nonce": nonce,
            "totp": totp,
            "key_fingerprint": fingerprint,
        })

        reply = self._recv_response(nonce, CONNECT_TIMEOUT)
        if reply is None or reply.get("status") != "ok":
            logger.error("Bridge authentication failed: %s", reply)
            return False

        # Verify bridge's TOTP (mutual auth)
        bridge_totp = reply.get("totp", "")
        if not _verify_totp(self._totp_secret, bridge_totp):
            logger.error("Bridge TOTP verification failed — possible MITM")
            self._close_socket()
            return False

        logger.info("Authenticated with Playnite bridge")
        self._last_activity = time.monotonic()
        return True

    def _send_message(self, msg: Dict[str, Any]) -> None:
        """Send a JSON message (newline-delimited)."""
        from luducat.plugins.sdk.json import json

        data = json.dumps(msg).encode("utf-8") + b"\n"
        if len(data) > MAX_MESSAGE_SIZE:
            raise ValueError(f"Message too large: {len(data)} bytes")
        self._sock.sendall(data)
        self._last_activity = time.monotonic()

    def _recv_response(
        self, expected_nonce: str, timeout: float
    ) -> Optional[Dict[str, Any]]:
        """Receive and parse a JSON response matching the expected nonce."""
        from luducat.plugins.sdk.json import json

        self._sock.settimeout(timeout)
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            # Check buffer for complete message
            newline_pos = self._buffer.find(b"\n")
            if newline_pos >= 0:
                line = self._buffer[:newline_pos]
                self._buffer = self._buffer[newline_pos + 1:]

                if len(line) > MAX_MESSAGE_SIZE:
                    logger.error("Received oversized message, dropping")
                    continue

                try:
                    msg = json.loads(line.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    logger.error("Received malformed JSON from bridge")
                    continue

                if msg.get("nonce") == expected_nonce:
                    self._last_activity = time.monotonic()
                    return msg
                else:
                    # Log unexpected messages (could be pong, etc.)
                    logger.debug("Unexpected message type=%s", msg.get("type"))
                    continue

            # Read more data
            try:
                remaining = max(0.1, deadline - time.monotonic())
                self._sock.settimeout(remaining)
                chunk = self._sock.recv(4096)
                if not chunk:
                    logger.error("Bridge connection closed")
                    self._close_socket()
                    return None
                self._buffer += chunk
            except socket.timeout:
                break

        logger.error("Timeout waiting for response nonce=%s", expected_nonce)
        return None

    def _close_socket(self) -> None:
        """Close the socket connection."""
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
            self._buffer = b""

    # ── Credential storage ────────────────────────────────────────────

    def _save_credentials(self) -> None:
        """Save pairing credentials to data directory."""
        from luducat.plugins.sdk.json import json
        import base64

        cred_path = self._data_dir / _CRED_FILENAME
        self._data_dir.mkdir(parents=True, exist_ok=True)

        data = {
            "protocol_version": PROTOCOL_VERSION,
            "peer_public_key": base64.b64encode(
                self._peer_public_key
            ).decode() if self._peer_public_key else None,
            "totp_secret": base64.b64encode(
                self._totp_secret
            ).decode() if self._totp_secret else None,
            "permissions": self._permissions,
            "server_cert_fingerprint": self._server_cert_fingerprint,
        }

        # Store our key material for reconnect signing
        if self._our_private_key is not None:
            from cryptography.hazmat.primitives.serialization import (
                Encoding, PrivateFormat, NoEncryption,
            )
            data["private_key"] = base64.b64encode(
                self._our_private_key.private_bytes(
                    Encoding.DER, PrivateFormat.PKCS8, NoEncryption()
                )
            ).decode()
            data["public_key"] = base64.b64encode(
                self._our_public_key
            ).decode() if self._our_public_key else None

        cred_path.write_text(json.dumps(data), encoding="utf-8")
        # Restrict file permissions on Unix
        try:
            cred_path.chmod(0o600)
        except OSError:
            pass

    def _load_credentials(self) -> None:
        """Load pairing credentials from data directory."""
        cred_path = self._data_dir / _CRED_FILENAME
        if not cred_path.is_file():
            return

        try:
            from luducat.plugins.sdk.json import json
            import base64

            data = json.loads(cred_path.read_text(encoding="utf-8"))

            peer_key_b64 = data.get("peer_public_key")
            if peer_key_b64:
                self._peer_public_key = base64.b64decode(peer_key_b64)

            totp_b64 = data.get("totp_secret")
            if totp_b64:
                self._totp_secret = base64.b64decode(totp_b64)

            self._permissions = data.get("permissions", [])
            self._server_cert_fingerprint = data.get("server_cert_fingerprint")

            priv_key_b64 = data.get("private_key")
            pub_key_b64 = data.get("public_key")
            if priv_key_b64 and pub_key_b64:
                from cryptography.hazmat.primitives.serialization import (
                    load_der_private_key,
                )
                self._our_private_key = load_der_private_key(
                    base64.b64decode(priv_key_b64), password=None,
                )
                self._our_public_key = base64.b64decode(pub_key_b64)

        except Exception as e:
            logger.warning("Failed to load bridge credentials: %s", e)

    def _delete_credentials(self) -> None:
        """Remove stored pairing credentials."""
        cred_path = self._data_dir / _CRED_FILENAME
        try:
            cred_path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Failed to delete bridge credentials: %s", e)
