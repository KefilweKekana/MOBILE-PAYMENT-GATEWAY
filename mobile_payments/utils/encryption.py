"""
Encryption Utilities
AES encryption for API key storage and sensitive data protection.
Uses PyCryptodome for AES-256-CBC encryption.
"""
from __future__ import unicode_literals

import base64
import hashlib
import os

import frappe


def _get_encryption_key():
    """
    Derive encryption key from Frappe's site encryption key.
    Returns a 32-byte key for AES-256.
    """
    site_key = frappe.local.conf.get("encryption_key") or frappe.utils.password.get_encryption_key()
    return hashlib.sha256(site_key.encode("utf-8")).digest()


def encrypt_value(plaintext):
    """
    Encrypt a plaintext string using AES-256-CBC.

    Args:
        plaintext: String to encrypt

    Returns:
        Base64-encoded encrypted string (IV + ciphertext)
    """
    if not plaintext:
        return ""

    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import pad
    except ImportError:
        frappe.throw(
            "PyCryptodome is required for encryption. "
            "Install via: pip install pycryptodome"
        )

    key = _get_encryption_key()
    iv = os.urandom(16)
    cipher = AES.new(key, AES.MODE_CBC, iv)

    plaintext_bytes = plaintext.encode("utf-8")
    padded = pad(plaintext_bytes, AES.block_size)
    ciphertext = cipher.encrypt(padded)

    # Combine IV + ciphertext and base64 encode
    encrypted = base64.b64encode(iv + ciphertext).decode("utf-8")
    return encrypted


def decrypt_value(encrypted_text):
    """
    Decrypt an AES-256-CBC encrypted string.

    Args:
        encrypted_text: Base64-encoded encrypted string (IV + ciphertext)

    Returns:
        Decrypted plaintext string
    """
    if not encrypted_text:
        return ""

    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad
    except ImportError:
        frappe.throw(
            "PyCryptodome is required for decryption. "
            "Install via: pip install pycryptodome"
        )

    key = _get_encryption_key()
    raw = base64.b64decode(encrypted_text)

    iv = raw[:16]
    ciphertext = raw[16:]

    cipher = AES.new(key, AES.MODE_CBC, iv)
    padded_plaintext = cipher.decrypt(ciphertext)
    plaintext = unpad(padded_plaintext, AES.block_size)

    return plaintext.decode("utf-8")


def hash_value(value):
    """
    Create a SHA-256 hash of a value. Useful for webhook signature validation.

    Args:
        value: String to hash

    Returns:
        Hex-encoded SHA-256 hash
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def generate_hmac_signature(message, secret):
    """
    Generate HMAC-SHA256 signature for webhook validation.

    Args:
        message: The message/payload to sign
        secret: The secret key

    Returns:
        Hex-encoded HMAC-SHA256 signature
    """
    import hmac

    return hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_hmac_signature(message, signature, secret):
    """
    Verify an HMAC-SHA256 signature (constant-time comparison).

    Args:
        message: The original message/payload
        signature: The signature to verify
        secret: The secret key

    Returns:
        Boolean indicating whether signature is valid
    """
    import hmac

    expected = generate_hmac_signature(message, secret)
    return hmac.compare_digest(expected, signature)
