"""HMAC 签名校验（docs/dev/03 第 4.4 节要求，docs/dev/05 第 2.1 节实现）。

复用于 Hermes Hook 与人工审批回调两类端点（各自使用不同的 secret）。
"""

from __future__ import annotations

import hashlib
import hmac


def verify_hmac_signature(raw_body: bytes, signature_header: str, secret: str) -> bool:
    if not signature_header or not secret:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


# 向后兼容别名：文档原文命名为 verify_hermes_signature。
verify_hermes_signature = verify_hmac_signature
