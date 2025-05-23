import uuid
import json
import numpy as np
from .documents.document_storage import DocumentStorage
from .embedding.embedding_storage import EmbeddingStorage
from ..provider.embedding import EmbeddingProvider
from dataclasses import dataclass
from loguru import logger


@dataclass
class Result:
    similarity: float
    data: dict


def uuid_to_int(uuid_str: str) -> int:
    """将UUID字符串转换为64位整数（只使用UUID的一部分）"""
    # 去掉连字符并截取前16位十六进制数（相当于64位整数）
    uuid_int = int(uuid_str.replace("-", "")[:16], 16)
    return uuid_int


class VecDB:
    """
    A class to represent a vector database.
    """

    def __init__(
        self,
        document_storage: DocumentStorage,
        embedding_storage: EmbeddingStorage,
        embedding_provider: EmbeddingProvider,
    ):
        self.document_storage = document_storage
        self.embedding_storage = embedding_storage
        self.embedding_provider = embedding_provider

    async def insert(
        self,
        content: str,
        metadata: dict = None,
        id: str = None,
    ) -> int:
        """
        插入一条文本和其对应向量，自动生成 ID 并保持一致性。
        """
        metadata = metadata or {}
        str_id = id or str(uuid.uuid4())  # 使用 UUID 作为原始 ID

        # 获取向量
        vector = await self.embedding_provider.get_embedding(content)
        vector = np.array(vector, dtype=np.float32)
        async with self.document_storage.connection.cursor() as cursor:
            await cursor.execute(
                "INSERT INTO documents (doc_id, text, metadata) VALUES (?, ?, ?)",
                (str_id, content, json.dumps(metadata)),
            )
            await self.document_storage.connection.commit()
            result = await self.document_storage.get_document_by_doc_id(str_id)
            int_id = result["id"]

        # 插入向量到 FAISS
        await self.embedding_storage.insert(vector, int_id)
        return int_id

    async def retrieve(
        self, query: str, k: int = 5, fetch_k: int = 20, metadata_filters: dict = None
    ) -> list[Result]:
        """
        搜索最相似的文档。

        Args:
            query (str): 查询文本
            k (int): 返回的最相似文档的数量
            fetch_k (int): 在根据 metadata 过滤前从 FAISS 中获取的数量
            metadata_filters (dict): 元数据过滤器

        Returns:
            List[Result]: 查询结果
        """
        embedding = await self.embedding_provider.get_embedding(query)
        scores, indices = await self.embedding_storage.search(
            vector=np.array([embedding]).astype("float32"),
            k=k if not metadata_filters else fetch_k,
        )
        # TODO: rerank
        if len(indices[0]) == 0 or indices[0][0] == -1:
            return []
        # normalize scores
        logger.debug(f"before similarity: {scores} indices: {indices}")
        scores[0] = 1.0 - (scores[0] / 2.0)
        logger.debug(f"retrieval from faiss: SIMILARITY {scores} INDICES {indices}")
        # NOTE: maybe the size is less than k.
        fetched_docs = await self.document_storage.get_documents(
            metadata_filters=metadata_filters or {}, ids=indices[0]
        )
        if not fetched_docs:
            return []
        result_docs = []

        idx_pos = {}
        for idx, fetch_doc in enumerate(fetched_docs):
            idx_pos[fetch_doc["id"]] = idx
        for i, indice_idx in enumerate(indices[0]):
            pos = idx_pos.get(indice_idx)
            if pos is None:
                continue
            fetch_doc = fetched_docs[pos]
            score = scores[0][i]
            result_docs.append(Result(similarity=score, data=fetch_doc))
        return result_docs[:k]

    async def delete(self, doc_id: int):
        """
        删除一条文档
        """
        await self.document_storage.connection.execute(
            "DELETE FROM documents WHERE doc_id = ?", (doc_id,)
        )
        await self.document_storage.connection.commit()

    async def close(self):
        await self.document_storage.close()
