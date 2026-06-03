# -*- coding: utf-8 -*-
"""
========================================================================
工具模块 2（生产级升级版）：基于 Milvus 的知识检索
========================================================================
把知识检索的向量后端从 FAISS（内存索引）升级为 Milvus（向量数据库）。

为什么升级（面试讲点）：
- FAISS 是内存索引库：每次启动要全量重建、数据不持久化、不支持增量更新，
  适合原型；数据一大就吃不消。
- Milvus 是向量数据库：数据持久化、支持增量插入/删除、可扩展到亿级向量、
  支持元数据过滤和分布式部署——是生产环境的标准选择，也是本岗位 JD 明确要求的。

★ 关键设计：本文件用 Milvus Lite（本地文件模式，pip 即可用，无需 Docker）。
  它与完整版 Milvus 共享同一套 API，迁移到分布式集群时，
  只需把 MilvusClient("xxx.db") 改成 MilvusClient(uri="http://集群地址:19530")，
  其余代码一行不用动。这就是"一套代码，从笔记本到生产集群"。

【运行前】
  pip install pymilvus -i https://pypi.tuna.tsinghua.edu.cn/simple
  设好 DASHSCOPE_API_KEY
  运行：python kb_tools_milvus.py
"""

import os
import glob
from openai import OpenAI
from pymilvus import MilvusClient

API_KEY = os.getenv("DASHSCOPE_API_KEY")
client_llm = OpenAI(api_key=API_KEY, base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
EMBED_MODEL = "text-embedding-v4"
EMBED_DIM = 1024

KB_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "knowledge")
MILVUS_DB = os.path.join(os.path.dirname(__file__), "milvus_kb.db")  # 本地向量库文件
COLLECTION = "knowledge_base"

CHUNK_SIZE = 150
CHUNK_OVERLAP = 30

# 全局 Milvus 客户端
_mc = None


def _get_client():
    global _mc
    if _mc is None:
        _mc = MilvusClient(MILVUS_DB)
    return _mc


def _embed(texts):
    """批量向量化 + 归一化。"""
    import numpy as np
    out = []
    for b in range(0, len(texts), 10):
        batch = texts[b:b + 10]
        resp = client_llm.embeddings.create(model=EMBED_MODEL, input=batch,
                                            dimensions=EMBED_DIM, encoding_format="float")
        for d in resp.data:
            v = np.array(d.embedding, dtype="float32")
            out.append((v / np.linalg.norm(v)).tolist())
    return out


def _load_and_chunk():
    chunks, sources = [], []
    for path in sorted(glob.glob(os.path.join(KB_DIR, "*.txt"))):
        fname = os.path.basename(path)
        with open(path, encoding="utf-8") as f:
            text = f.read().replace("\n", "")
        start = 0
        while start < len(text):
            piece = text[start:start + CHUNK_SIZE]
            if piece.strip():
                chunks.append(piece); sources.append(fname)
            start += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks, sources


def build_collection(rebuild=False):
    """构建 Milvus 集合并灌入知识库向量。"""
    mc = _get_client()
    if mc.has_collection(COLLECTION):
        if rebuild:
            mc.drop_collection(COLLECTION)
        else:
            return  # 已存在，直接复用（数据持久化的好处）
    # 创建集合（指定向量维度；启用动态字段以存 text/source 等元数据）
    mc.create_collection(collection_name=COLLECTION, dimension=EMBED_DIM,
                         metric_type="IP", auto_id=True)
    chunks, sources = _load_and_chunk()
    vecs = _embed(chunks)
    data = [{"vector": vecs[i], "text": chunks[i], "source": sources[i]}
            for i in range(len(chunks))]
    mc.insert(collection_name=COLLECTION, data=data)
    print(f"Milvus 集合已构建：插入 {len(data)} 条知识向量。")


def search_knowledge_base(query: str, top_k: int = 3) -> str:
    """根据问题检索企业知识库（Milvus 向量检索），返回最相关内容及来源。"""
    mc = _get_client()
    build_collection()  # 确保集合存在（已存在则跳过，体现持久化）
    qv = _embed(["为这个句子生成表示以用于检索相关文章：" + query])
    hits = mc.search(collection_name=COLLECTION, data=qv, limit=top_k,
                     output_fields=["text", "source"])[0]
    lines = [f"知识库检索结果（问题：{query}）："]
    for rank, h in enumerate(hits, 1):
        ent = h["entity"]
        lines.append(f"  [{rank}] 来源《{ent['source']}》(相关度{h['distance']:.3f})：{ent['text']}")
    return "\n".join(lines)


if __name__ == "__main__":
    if not API_KEY:
        raise SystemExit("请先设置 DASHSCOPE_API_KEY")
    print("构建 Milvus 知识库（首次会调用 embedding）…")
    build_collection(rebuild=True)
    print()
    for q in ["刻蚀机良率下降、颗粒污染，有没有类似的历史处理案例？",
              "良率异常应该按什么步骤排查？",
              "颗粒计数超过多少需要立即停机？"]:
        print("=" * 60)
        print(search_knowledge_base(q, top_k=2))
        print()
    print("✅ Milvus 版知识检索测试完成。注意：milvus_kb.db 已持久化，再次运行无需重建。")
