"""临时：走真实登录链拿 refresh_token（仅供本地无头截图测试用）。"""
import base64
import hashlib
import json
import sys

import httpx
from Crypto.Cipher import AES

sys.stdout.reconfigure(encoding="utf-8")

KEY = b"16bit secret key"


def encrypt_password(data: str) -> str:
    iv = hashlib.sha256(KEY).hexdigest()[:16].encode()
    raw = data.encode()
    pad_len = 16 - (len(raw) % 16)
    raw += bytes([pad_len]) * pad_len
    return base64.b64encode(AES.new(KEY, AES.MODE_CBC, iv).encrypt(raw)).decode()


def main():
    email, password = sys.argv[1], sys.argv[2]
    with httpx.Client(timeout=20, trust_env=False) as hc:
        r = hc.post("https://ssemarket.cn/api/auth/login", json={
            "email": email,
            "password": encrypt_password(password),
        })
        data = r.json()
        tok = (data.get("data") or {})
        if not tok.get("refresh_token"):
            print(json.dumps(data, ensure_ascii=False)[:300])
            sys.exit(1)
        print(json.dumps({
            "token_head": tok["token"][:20],
            "refresh_token": tok["refresh_token"],
        }))


if __name__ == "__main__":
    main()
