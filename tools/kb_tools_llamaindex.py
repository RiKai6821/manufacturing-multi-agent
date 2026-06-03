# -*- coding: utf-8 -*-
"""
知识检索的 LlamaIndex 实现（对照手写版 kb_tools.py）
========================================================================
同样是"文档→切分→向量化→检索"的 RAG，这次用 LlamaIndex 框架实现，
对比手写版看框架替我们封装了什么。

【手写版(kb_tools.py) ←→ 框架版(本文件) 对照】
  手写 _load_and_chunk 滑窗切分    →  SimpleDirectoryReader + SentenceSplitter
  手写 _embed 批量向量化           →  自定义 Embedding 类，框架自动批量调用
  手写 faiss.IndexFlatIP + 缓存    →  VectorStoreIndex 自动管理索引
  手写 search 相似度+去重+过滤      →  index.as_retriever(similarity_top_k=k)
  手写返回格式拼接                  →  retriever 返回 NodeWithScore，带 score 和 metadata

  结论：LlamaIndex 把 RAG 全流程组件化，几行搭一个检索器；
  但手写版的置信度过滤、按来源去重、缓存配置校验等定制逻辑，
  框架要做需用 postprocessor / 自定义 retriever 扩展——懂原理才知道往哪扩。

【关键技巧】百炼无官方 LlamaIndex embedding，这里自定义 BaseEmbedding 子类接入，
  说明：框架的可扩展点（自定义组件）是工程落地的关键，不能只会用默认件。

运行：python kb_tools_llamaindex.py
"""

import os
import sys
from typing import List

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config  # 自动加载 DASHSCOPE_API_KEY
from settings import settings

import numpy as np
from openai import OpenAI
from llama_index.core import VectorStoreIndex, Document, Settings
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.embeddings import BaseEmbedding

import glob

_client = OpenAI(api_key=os.getenv("DASHSCOPE_API_KEY"),
                 base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
KB_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "knowledge")


# ════════════════════════════════════════════════════════════
# 自定义 Embedding：把百炼 text-embedding-v4 接入 LlamaIndex
# 框架的核心扩展点——实现 BaseEmbedding 的几个抽象方法即可无缝接入
# ════════════════════════════════════════════════════════════

class DashScopeEmbedding(BaseEmbedding):
    """百炼 embedding 的 LlamaIndex 适配器。"""

    def _embed_one(self, text: str) -> List[float]:
        resp = _client.embeddings.create(
            model=settings.embed_model, input=[text],
            dimensions=settings.embed_dim, encoding_format="float")
        v = np.array(resp.data[0].embedding, dtype="float32")
        return (v / np.linalg.norm(v)).tolist()   # 归一化

    def _get_query_embedding(self, query: str) -> List[float]:
        # 查询前缀，和手写版一致，提升检索准确率
        return self._embed_one("为这个句子生成表示以用于检索相关文章：" + query)

    def _get_text_embedding(self, text: str) -> List[float]:
        return self._embed_one(text)

    async def _aget_query_embedding(self, query: str) -> List[float]:
        return self._get_query_embedding(query)

    async def _aget_text_embedding(self, text: str) -> List[float]:
        return self._get_text_embedding(text)


# ════════════════════════════════════════════════════════════
# 构建索引（框架几行搞定 加载→切分→向量化→建索引）
# ════════════════════════════════════════════════════════════

_index = None


def _ensure_index():
    global _index
    if _index is not None:
        return

    # 配置全局 embedding（用我们的百炼适配器，禁用默认OpenAI）
    Settings.embed_model = DashScopeEmbedding()
    Settings.llm = None   # 只做检索，不用 LlamaIndex 内置LLM

    # 1. 加载知识库文档（每个txt一个Document）
    docs = []
    for path in sorted(glob.glob(os.path.join(KB_DIR, "*.txt"))):
        with open(path, encoding="utf-8") as f:
            docs.append(Document(text=f.read(),
                                 metadata={"source": os.path.basename(path)}))

    # 2. 切分器（对应手写版的滑窗切分）
    splitter = SentenceSplitter(chunk_size=settings.chunk_size,
                                chunk_overlap=settings.chunk_overlap)

    # 3. 一行构建向量索引（框架自动：切分→批量embedding→建索引）
    _index = VectorStoreIndex.from_documents(docs, transformations=[splitter])


# ════════════════════════════════════════════════════════════
# 检索工具（对外接口，与手写版 search_knowledge_base 对齐）
# ════════════════════════════════════════════════════════════

def search_knowledge_base(query: str, top_k: int = None) -> str:
    """检索企业知识库，返回最相关内容及来源（LlamaIndex 版）。"""
    if not query or not query.strip():
        return "错误：检索问题不能为空。"
    top_k = top_k or settings.top_k

    _ensure_index()
    retriever = _index.as_retriever(similarity_top_k=top_k)
    nodes = retriever.retrieve(query)

    # 置信度过滤（对应手写版的 min_score）
    nodes = [n for n in nodes if (n.score or 0) >= settings.min_score]
    if not nodes:
        return f"知识库中未找到与「{query}」相关度足够高的内容（阈值{settings.min_score}）。"

    lines = [f"知识库检索结果（问题：{query}，命中{len(nodes)}条）："]
    for rank, n in enumerate(nodes, 1):
        src = n.metadata.get("source", "未知")
        lines.append(f"  [{rank}] 来源《{src}》(相关度{n.score:.3f})：{n.text[:120]}")
    return "\n".join(lines)


if __name__ == "__main__":
    if not os.getenv("DASHSCOPE_API_KEY"):
        raise SystemExit("请先设置 DASHSCOPE_API_KEY")
    print("构建 LlamaIndex 索引（调用百炼 embedding）…")
    _ensure_index()
    print("索引就绪。\n")

    for q in [
        "刻蚀机良率下降、颗粒污染，有没有类似的历史处理案例？",
        "颗粒计数超过多少需要立即停机？",
        "今天午饭吃什么？",   # 测试置信度过滤
    ]:
        print("=" * 60)
        print(search_knowledge_base(q, top_k=2))
        print()
