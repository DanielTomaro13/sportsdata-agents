#!/usr/bin/env python3
"""Generate the Ed25519 keypair for the entitlement service (run once).

  - PRIVATE key → PKCS8 base64  → Worker secret `SIGNING_KEY_PKCS8_B64`
  - PUBLIC  key → raw base64url → bake into the MCP for offline verification

Keep the private output OUT of git / chat — set it straight into the Worker secret.
"""

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

k = Ed25519PrivateKey.generate()
pkcs8 = k.private_bytes(
    serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
)
pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

print("# PRIVATE — set as a Worker secret, never commit:")
print("#   wrangler secret put SIGNING_KEY_PKCS8_B64")
print(base64.b64encode(pkcs8).decode())
print()
print("# PUBLIC — bake into the MCP (raw base64url) to verify licences offline:")
print(base64.urlsafe_b64encode(pub).rstrip(b"=").decode())
