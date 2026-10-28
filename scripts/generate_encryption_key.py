"""Generate a Fernet key for the ENCRYPTION_KEY setting.

Usage:
    python scripts/generate_encryption_key.py

Copy the printed value into your backend .env file (or deployment
environment) as:

    ENCRYPTION_KEY=<printed value>

The key is a URL-safe base64-encoded 32-byte value used by Fernet
(AES-128-CBC + HMAC-SHA256) to encrypt LLM provider API keys at rest.
Keep it secret and stable: rotating it makes previously stored API keys
undecryptable (rotate deliberately with MultiFernet instead).
"""

from cryptography.fernet import Fernet

if __name__ == "__main__":
    print(Fernet.generate_key().decode())
