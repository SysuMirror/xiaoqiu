"""Qdrant 连通性探测。

用法:
  python scripts/qdrant_probe.py <host> <port> <api_key> [prefix]
示例（拿到公网入口后，key 从 .env 或老师处获取）:
  python scripts/qdrant_probe.py 1.2.3.4 6333 <api_key> gb1ca3490
在实验环境内则用原始域名:
  python scripts/qdrant_probe.py qdrant.platform-infra.svc.cluster.local 6333 <api_key> gb1ca3490
"""
import sys

from qdrant_client import QdrantClient


def main() -> int:
    host = sys.argv[1] if len(sys.argv) > 1 else "qdrant.platform-infra.svc.cluster.local"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 6333
    api_key = sys.argv[3] if len(sys.argv) > 3 else ""
    prefix = sys.argv[4] if len(sys.argv) > 4 else "gb1ca3490"

    print(f"[1] connecting http://{host}:{port} ...")
    try:
        qc = QdrantClient(host=host, port=port, api_key=api_key or None, https=False, timeout=10)
    except Exception as e:  # noqa: BLE001
        print(f"FAIL connect: {type(e).__name__}: {e}")
        return 1

    try:
        collections = qc.get_collections().collections
        print(f"[2] OK, auth OK, collections: {[c.name for c in collections]}")
    except Exception as e:  # noqa: BLE001
        print(f"FAIL auth/list: {type(e).__name__}: {e}")
        return 1

    mine = [c.name for c in collections if c.name.startswith(prefix + "_")]
    print(f"[3] 本组前缀 {prefix}_* 的集合: {mine}")

    test_name = f"{prefix}_connectivity_test"
    from qdrant_client.models import Distance, VectorParams

    try:
        qc.recreate_collection(test_name, vectors_config=VectorParams(size=4, distance=Distance.COSINE))
        qc.delete_collection(test_name)
        print(f"[4] 写删权限 OK (建/删测试集合 {test_name} 成功)")
    except Exception as e:  # noqa: BLE001
        print(f"[4] 写权限受限: {type(e).__name__}: {e}")
    print("\nPROBE PASS — 可以接入 rag/store.py 的 qdrant 后端")
    return 0


if __name__ == "__main__":
    sys.exit(main())
