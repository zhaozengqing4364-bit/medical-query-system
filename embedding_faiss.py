"""
UDID 向量检索服务 - FAISS 版本
==============================

使用 FAISS 实现高效的近似最近邻 (ANN) 搜索。
支持 255 万向量，搜索延迟 < 50ms。

架构：
- FAISS Index: 存储向量，支持 ANN 搜索
- SQLite: 存储产品元数据和 di_code 映射

版本: 1.0.0
"""

import os
import json
import pickle
import sqlite3
import numpy as np
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from db_backend import connect as db_connect

# 配置
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
DB_PATH = os.path.join(os.path.dirname(__file__), "udid_hybrid_lake.db")
FAISS_INDEX_PATH = os.path.join(os.path.dirname(__file__), "data", "faiss_index")


def _load_config() -> Dict:
    """加载配置"""
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def get_embedding_dim() -> int:
    """获取向量维度（从 config.json 读取）"""
    config = _load_config()
    return config.get("embedding_dim", 1024)


def optimize_sqlite_connection(conn: sqlite3.Connection):
    """
    优化 SQLite 连接性能（适用于 14GB+ 大数据库）
    - WAL 模式：提高并发读写性能
    - mmap_size：利用内存映射减少磁盘 IO
    """
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA mmap_size=2147483648")  # 2GB mmap
    cursor.execute("PRAGMA cache_size=-262144")  # 256MB page cache
    cursor.execute("PRAGMA synchronous=NORMAL")  # 平衡安全性和性能
    cursor.close()


# ==========================================
# FAISS 索引管理
# ==========================================


class FAISSIndex:
    """FAISS 向量索引封装"""

    def __init__(self, index_path: str = FAISS_INDEX_PATH):
        self.index_path = index_path
        self.index = None
        self.id_map = {}  # faiss_id -> di_code
        self.reverse_map = {}  # di_code -> faiss_id

        # 确保目录存在
        os.makedirs(index_path, exist_ok=True)

        # 尝试加载已有索引
        self._load()

    def _load(self):
        """加载索引"""
        index_file = os.path.join(self.index_path, "index.faiss")
        map_file = os.path.join(self.index_path, "id_map.json")
        legacy_map_file = os.path.join(self.index_path, "id_map.pkl")
        manifest_file = os.path.join(self.index_path, "manifest.json")

        if os.path.exists(index_file) and not os.path.exists(map_file) and os.path.exists(legacy_map_file):
            migrated = self._migrate_legacy_id_map(index_file, legacy_map_file, map_file, manifest_file)
            if not migrated:
                print("[FAISS] 旧版映射迁移失败，请执行完整重建。")
                self.index = None
                return

        if os.path.exists(index_file) and os.path.exists(map_file):
            try:
                import faiss

                self.index = faiss.read_index(index_file)
                with open(map_file, "r", encoding="utf-8") as f:
                    raw_map = json.load(f)
                self.id_map = {int(k): str(v) for k, v in raw_map.items()}
                if not isinstance(self.id_map, dict):
                    raise RuntimeError("id_map 格式错误")
                self.reverse_map = {v: k for k, v in self.id_map.items()}
                if self.index.ntotal != len(self.id_map):
                    raise RuntimeError(
                        f"索引与映射数量不一致: ntotal={self.index.ntotal}, map={len(self.id_map)}"
                    )
                if os.path.exists(manifest_file):
                    with open(manifest_file, "r", encoding="utf-8") as f:
                        manifest = json.load(f)
                    expected = int(manifest.get("vector_count", -1))
                    if expected >= 0 and expected != self.index.ntotal:
                        raise RuntimeError(
                            f"manifest 向量数量不一致: manifest={expected}, index={self.index.ntotal}"
                        )
                print(f"[FAISS] 加载索引成功，共 {self.index.ntotal} 个向量")
            except Exception as e:
                print(f"[FAISS] 加载索引失败: {e}")
                self.index = None
        elif os.path.exists(index_file) and os.path.exists(legacy_map_file):
            print("[FAISS] 检测到旧版 id_map.pkl，为安全起见不再加载。请重建索引以生成 id_map.json。")
            self.index = None

    def _load_legacy_id_map(self, legacy_map_file: str) -> Dict[int, str]:
        """读取旧版 pkl 映射并规范化为 {int: str}。"""
        with open(legacy_map_file, "rb") as f:
            raw_map = pickle.load(f)
        if not isinstance(raw_map, dict):
            raise RuntimeError(f"旧版 id_map.pkl 格式错误: {type(raw_map).__name__}")
        normalized = {}
        for key, value in raw_map.items():
            normalized[int(key)] = str(value)
        return normalized

    def _write_map_and_manifest(
        self, map_file: str, manifest_file: str, id_map: Dict[int, str], vector_count: int
    ):
        """原子写入映射和 manifest。"""
        map_tmp = f"{map_file}.tmp"
        manifest_tmp = f"{manifest_file}.tmp"
        manifest = {
            "vector_count": int(vector_count),
            "map_count": int(len(id_map)),
            "updated_at": datetime.utcnow().isoformat() + "Z",
            "generated_by": "legacy-id-map-migration",
        }
        try:
            with open(map_tmp, "w", encoding="utf-8") as f:
                json.dump({str(k): v for k, v in id_map.items()}, f, ensure_ascii=False)
            with open(manifest_tmp, "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)
            os.replace(map_tmp, map_file)
            os.replace(manifest_tmp, manifest_file)
        except Exception:
            for tmp_path in (map_tmp, manifest_tmp):
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
            raise

    def _migrate_legacy_id_map(
        self, index_file: str, legacy_map_file: str, map_file: str, manifest_file: str
    ) -> bool:
        """将旧版 id_map.pkl 迁移为 id_map.json + manifest.json。"""
        try:
            import faiss

            index = faiss.read_index(index_file)
            id_map = self._load_legacy_id_map(legacy_map_file)
            if index.ntotal != len(id_map):
                raise RuntimeError(
                    f"旧版索引与映射数量不一致: ntotal={index.ntotal}, map={len(id_map)}"
                )
            self._write_map_and_manifest(map_file, manifest_file, id_map, index.ntotal)
            print(f"[FAISS] 已迁移旧版 id_map.pkl -> id_map.json，共 {len(id_map)} 条映射")
            return True
        except Exception as e:
            print(f"[FAISS] 迁移旧版 id_map 失败: {e}")
            return False

    def _save(self):
        """保存索引"""
        if self.index is None:
            return

        try:
            import faiss

            index_file = os.path.join(self.index_path, "index.faiss")
            map_file = os.path.join(self.index_path, "id_map.json")
            manifest_file = os.path.join(self.index_path, "manifest.json")

            if self.index.ntotal != len(self.id_map):
                raise RuntimeError(
                    f"保存前一致性校验失败: ntotal={self.index.ntotal}, id_map={len(self.id_map)}"
                )

            index_tmp = f"{index_file}.tmp"
            faiss.write_index(self.index, index_tmp)
            self._write_map_and_manifest(map_file, manifest_file, self.id_map, self.index.ntotal)
            os.replace(index_tmp, index_file)

            print(f"[FAISS] 保存索引成功")
        except Exception as e:
            index_tmp = locals().get("index_tmp")
            if index_tmp and os.path.exists(index_tmp):
                try:
                    os.remove(index_tmp)
                except Exception:
                    pass
            print(f"[FAISS] 保存索引失败: {e}")

    def build_from_db(self, conn: sqlite3.Connection, batch_size: int = 50000):
        """
        从数据库构建 FAISS 索引

        使用 IVF + PQ 索引，适合百万级向量：
        - IVF (Inverted File): 将向量聚类，搜索时只查相关簇
        - PQ (Product Quantization): 压缩向量，减少内存
        """
        import faiss
        import time

        start_time = time.time()

        cursor = conn.cursor()

        # 获取向量总数（仅统计非空向量）
        cursor.execute("SELECT COUNT(*) FROM embeddings WHERE embedding IS NOT NULL")
        total = cursor.fetchone()[0]
        print(f"[FAISS] 开始构建索引，共 {total} 个非空向量")

        if total == 0:
            print("[FAISS] 没有向量数据")
            return {"success": False, "error": "没有向量数据"}

        # 选择索引类型
        embedding_dim = get_embedding_dim()
        if total < 10000:
            # 小数据集：使用 Flat 索引（精确搜索）
            print("[FAISS] 使用 Flat 索引（精确搜索）")
            self.index = faiss.IndexFlatIP(
                embedding_dim
            )  # 内积（余弦相似度需要归一化）
        elif total < 100000:
            # 中等数据集：使用 IVF
            nlist = int(np.sqrt(total))  # 聚类数
            print(f"[FAISS] 使用 IVF 索引，nlist={nlist}")
            quantizer = faiss.IndexFlatIP(embedding_dim)
            self.index = faiss.IndexIVFFlat(
                quantizer, embedding_dim, nlist, faiss.METRIC_INNER_PRODUCT
            )
        else:
            # 大数据集：使用 IVF + PQ（优化参数）
            nlist = min(8192, int(np.sqrt(total)))  # 增加聚类数
            m = 128  # 增加子向量数提升精度（1024/128=8，每个子向量8维）
            print(f"[FAISS] 使用 IVF+PQ 索引（优化版），nlist={nlist}, m={m}")
            quantizer = faiss.IndexFlatIP(embedding_dim)
            self.index = faiss.IndexIVFPQ(quantizer, embedding_dim, nlist, m, 8)

        # 收集训练数据（用于 IVF 聚类）
        train_size = min(100000, total)
        print(f"[FAISS] 收集训练数据 {train_size} 条...")

        cursor.execute("SELECT embedding FROM embeddings WHERE embedding IS NOT NULL LIMIT ?", (train_size,))
        train_vectors = []
        while len(train_vectors) < train_size:
            rows = cursor.fetchmany(2000)
            if not rows:
                break
            for row in rows:
                if row[0] is None:
                    continue
                vec = np.frombuffer(row[0], dtype=np.float32)
                # 归一化（用于内积 = 余弦相似度）
                norm = np.linalg.norm(vec)
                if norm > 0:
                    vec = vec / norm
                train_vectors.append(vec)

        train_data = np.asarray(train_vectors, dtype=np.float32)
        if train_data.size == 0:
            return {"success": False, "error": "训练样本为空"}

        # 训练索引
        if hasattr(self.index, "train"):
            print("[FAISS] 训练索引...")
            self.index.train(train_data)

        # 分批添加向量
        print("[FAISS] 添加向量...")
        self.id_map = {}
        faiss_id = 0

        cursor.execute("SELECT di_code, embedding FROM embeddings WHERE embedding IS NOT NULL")
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break

            batch_vectors = []
            for di_code, emb_blob in rows:
                if emb_blob is None:
                    continue
                vec = np.frombuffer(emb_blob, dtype=np.float32)
                # 归一化
                norm = np.linalg.norm(vec)
                if norm > 0:
                    vec = vec / norm
                batch_vectors.append(vec)
                self.id_map[faiss_id] = di_code
                faiss_id += 1

            if batch_vectors:
                vectors = np.asarray(batch_vectors, dtype=np.float32)
                self.index.add(vectors)
            print(f"[FAISS] 已添加 {faiss_id} / {total}")

        self.reverse_map = {v: k for k, v in self.id_map.items()}

        # 保存索引
        self._save()

        elapsed = time.time() - start_time
        print(
            f"[FAISS] 索引构建完成，共 {self.index.ntotal} 个向量，耗时 {elapsed:.1f}s"
        )

        return {"success": True, "total": self.index.ntotal, "elapsed": elapsed}

    def search(
        self, query_vector: np.ndarray, top_k: int = 50
    ) -> List[Tuple[str, float]]:
        """
        搜索最相似的向量

        Args:
            query_vector: 查询向量 (已归一化)
            top_k: 返回数量

        Returns:
            [(di_code, similarity), ...]
        """
        if self.index is None:
            print("[FAISS] 索引未加载")
            return []

        # 确保是 2D 数组
        if query_vector.ndim == 1:
            query_vector = query_vector.reshape(1, -1)

        # 归一化
        norm = np.linalg.norm(query_vector)
        if norm > 0:
            query_vector = query_vector / norm

        query_vector = query_vector.astype(np.float32)

        # 设置搜索参数（IVF 索引需要）
        if hasattr(self.index, "nprobe"):
            # 根据索引类型设置不同的 nprobe
            import faiss

            if isinstance(self.index, faiss.IndexIVFPQ):
                # PQ 索引需要更积极的探查以补偿量化误差
                self.index.nprobe = min(128, max(64, self.index.nlist // 8))
                print(f"[FAISS] PQ索引设置 nprobe={self.index.nprobe}")
            else:
                # IVFFlat 可以用较小的 nprobe
                self.index.nprobe = min(64, max(32, self.index.nlist // 16))

        # 搜索
        distances, indices = self.index.search(query_vector, top_k)

        results = []
        for i, idx in enumerate(indices[0]):
            if idx >= 0 and idx in self.id_map:
                di_code = self.id_map[idx]
                similarity = float(distances[0][i])  # 内积 = 余弦相似度（已归一化）
                results.append((di_code, similarity))

        return results


# 全局索引实例
_faiss_index = None


def get_faiss_index() -> FAISSIndex:
    """获取 FAISS 索引单例"""
    global _faiss_index
    if _faiss_index is None:
        _faiss_index = FAISSIndex()
    return _faiss_index


# ==========================================
# 向量检索接口
# ==========================================


def faiss_search(
    query: str, conn: sqlite3.Connection = None, top_k: int = 50, filters: Dict = None
) -> List[Dict]:
    """
    使用 FAISS 进行向量检索（召回后重排）

    Args:
        query: 用户查询文本
        conn: 数据库连接
        top_k: 返回数量
        filters: 筛选条件（后过滤）

    Returns:
        产品列表，带有 similarity 字段
    """
    import time

    start_time = time.time()

    if conn is None:
        conn = db_connect(DB_PATH)
        optimize_sqlite_connection(conn)

    # 获取 FAISS 索引
    faiss_idx = get_faiss_index()

    if faiss_idx.index is None:
        print("[FAISS] 索引未构建，请先运行 build_faiss_index()")
        return []

    # 获取查询向量
    from embedding_service import get_single_embedding, load_config

    config = load_config()
    query_embedding = get_single_embedding(query, config)

    if query_embedding is None:
        print("[FAISS] 无法获取查询向量")
        return []

    query_vector = np.array(query_embedding, dtype=np.float32)

    # 归一化查询向量
    query_norm = np.linalg.norm(query_vector)
    if query_norm > 0:
        query_vector_normalized = query_vector / query_norm
    else:
        query_vector_normalized = query_vector

    # FAISS 搜索（扩大召回量，用于后过滤和重排）
    recall_k = top_k * 20 if filters else top_k * 10

    search_start = time.time()
    faiss_results = faiss_idx.search(query_vector_normalized, recall_k)
    search_time = (time.time() - search_start) * 1000

    print(f"[FAISS] 向量搜索 {len(faiss_results)} 条，耗时 {search_time:.1f}ms")

    if not faiss_results:
        return []

    # 获取产品详情（直接使用 FAISS 返回的相似度，避免回表读取向量）
    di_codes = [r[0] for r in faiss_results]
    faiss_scores = {r[0]: r[1] for r in faiss_results}  # di_code -> similarity

    # 构建过滤条件（IN 查询将分块执行，避免 SQLite 变量上限）
    filter_conditions = []
    filter_params = []

    if filters:
        if filters.get("category_code"):
            filter_conditions.append("p.category_code LIKE ?")
            filter_params.append(f"{filters['category_code']}%")
        if filters.get("manufacturer"):
            filter_conditions.append("p.manufacturer LIKE ?")
            filter_params.append(f"%{filters['manufacturer']}%")
        if filters.get("keyword"):
            filter_conditions.append("(p.product_name LIKE ? OR p.description LIKE ?)")
            filter_params.extend([f"%{filters['keyword']}%"] * 2)

    cursor = conn.cursor()

    # 只获取产品元数据（不读取向量，避免 14GB 数据库的随机 IO）
    rows = []
    in_batch_size = 500
    for i in range(0, len(di_codes), in_batch_size):
        batch_codes = di_codes[i:i + in_batch_size]
        if not batch_codes:
            continue
        placeholders = ",".join(["?" for _ in batch_codes])
        where_conditions = [f"p.di_code IN ({placeholders})"] + filter_conditions
        where_sql = " AND ".join(where_conditions)
        params = list(batch_codes) + list(filter_params)
        cursor.execute(
            f"""
            SELECT p.di_code, p.product_name, p.commercial_name, p.model, p.manufacturer,
                   p.description, p.publish_date, p.source, p.last_updated, p.category_code, p.scope
            FROM products p
            WHERE {where_sql}
            """,
            params,
        )
        rows.extend(cursor.fetchall())

    # 组装结果，直接使用 FAISS 返回的相似度分数
    columns = [
        "di_code",
        "product_name",
        "commercial_name",
        "model",
        "manufacturer",
        "description",
        "publish_date",
        "source",
        "last_updated",
        "category_code",
        "scope",
    ]

    results = []
    for row in rows:
        item = dict(zip(columns, row))
        # 直接使用 FAISS 的内积分数（向量已归一化，内积 = 余弦相似度）
        item["similarity"] = faiss_scores.get(item["di_code"], 0.0)
        results.append(item)

    # 按 FAISS 相似度排序
    results.sort(key=lambda x: x["similarity"], reverse=True)

    total_time = (time.time() - start_time) * 1000
    print(
        f"[FAISS] 检索完成，返回 {len(results[:top_k])} 条，总耗时 {total_time:.0f}ms"
    )

    return results[:top_k]


def build_faiss_index(conn: sqlite3.Connection = None) -> Dict:
    """构建 FAISS 索引"""
    if conn is None:
        conn = db_connect(DB_PATH)

    faiss_idx = get_faiss_index()
    return faiss_idx.build_from_db(conn)


# ==========================================
# 命令行工具
# ==========================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="UDID FAISS 向量检索")
    parser.add_argument("--build", action="store_true", help="构建 FAISS 索引")
    parser.add_argument("--search", type=str, help="测试搜索")
    parser.add_argument("--stats", action="store_true", help="显示统计信息")

    args = parser.parse_args()

    conn = db_connect(DB_PATH)
    optimize_sqlite_connection(conn)

    if args.build:
        print("开始构建 FAISS 索引...")
        result = build_faiss_index(conn)
        print(f"结果: {result}")

    elif args.search:
        print(f"搜索: {args.search}")
        results = faiss_search(args.search, conn, top_k=10)
        print(f"\n找到 {len(results)} 个结果:\n")
        for i, r in enumerate(results, 1):
            print(f"{i}. [{r['similarity']:.3f}] {r['product_name']}")
            print(f"   厂家: {r['manufacturer']}")
            print(f"   规格: {r['model']}")
            print()

    elif args.stats:
        faiss_idx = get_faiss_index()
        if faiss_idx.index:
            print(f"FAISS 索引向量数: {faiss_idx.index.ntotal}")
            print(f"ID 映射数: {len(faiss_idx.id_map)}")
        else:
            print("FAISS 索引未构建")

    else:
        parser.print_help()

    conn.close()
