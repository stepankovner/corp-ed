"""Программный ключ доступа (WebAuthn) для тестов: P-256, формат «none».

Делает то же, что браузер с отпечатком или Face ID: на регистрации —
attestationObject и clientDataJSON, на входе — подпись challenge. Так
проверяется весь путь ключа доступа без настоящего устройства.
"""

import hashlib
import json
import secrets
import struct
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, encode_cbor


class SoftAuthenticator:
    def __init__(self, rp_id: str = "test", origin: str = "https://test") -> None:
        self.rp_id = rp_id
        self.origin = origin
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(32)
        self.sign_count = 0

    def _cose_key(self) -> bytes:
        numbers = self.key.public_key().public_numbers()
        return encode_cbor(
            {
                1: 2,  # kty: EC2
                3: -7,  # alg: ES256
                -1: 1,  # crv: P-256
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )

    def _client_data(self, kind: str, challenge: str) -> bytes:
        return json.dumps(
            {"type": kind, "challenge": challenge, "origin": self.origin}
        ).encode()

    def register(self, options: dict[str, Any]) -> dict[str, Any]:
        client_data = self._client_data("webauthn.create", options["challenge"])
        auth_data = (
            hashlib.sha256(self.rp_id.encode()).digest()
            + bytes([0x45])  # UP | UV | AT
            + struct.pack(">I", self.sign_count)
            + bytes(16)  # AAGUID
            + struct.pack(">H", len(self.credential_id))
            + self.credential_id
            + self._cose_key()
        )
        attestation = encode_cbor({"fmt": "none", "attStmt": {}, "authData": auth_data})
        raw_id = bytes_to_base64url(self.credential_id)
        return {
            "id": raw_id,
            "rawId": raw_id,
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "attestationObject": bytes_to_base64url(attestation),
                "transports": ["internal"],
            },
        }

    def sign(self, options: dict[str, Any]) -> dict[str, Any]:
        self.sign_count += 1
        client_data = self._client_data("webauthn.get", options["challenge"])
        auth_data = (
            hashlib.sha256(self.rp_id.encode()).digest()
            + bytes([0x05])  # UP | UV
            + struct.pack(">I", self.sign_count)
        )
        signature = self.key.sign(
            auth_data + hashlib.sha256(client_data).digest(),
            ec.ECDSA(hashes.SHA256()),
        )
        raw_id = bytes_to_base64url(self.credential_id)
        return {
            "id": raw_id,
            "rawId": raw_id,
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "authenticatorData": bytes_to_base64url(auth_data),
                "signature": bytes_to_base64url(signature),
                "userHandle": None,
            },
        }


__all__ = ["SoftAuthenticator", "base64url_to_bytes"]
