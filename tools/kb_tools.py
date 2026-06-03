# -*- coding: utf-8 -*-
"""
工具模块 2：知识检索工具（Agentic RAG 核心，对接企业知识库）
工程化改造 v2.0：
  - 置信度过滤：低于 min_score 的检索结果直接丢弃，不返回低质量内容
  - 结果去重：同一来源文件的多个相似 chunk 只保留最相关的，避免冗余
  - embedding API 失败自动重试（带指数退避）
  - 参数/配置统一从 settings 读取
  - 结构化日志替代 print
  - 索引元信息校验：chunk 配置变化时自动失效旧缓存

链路：读取文档 → 切分 → 向量化 → 建 FAISS 索引（缓存）→ 置信度过滤检索 → 去重
对应 JD：「掌握 RAG 原理，结合企业知识库优化输出准确性」
"""

import os
import sys
import time
import glob
import pickle
import numpy as np
import faiss
from openai import OpenAI

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config  # 自动加载 DASHSCOPE_API_KEY
from settings import settings
from logger_config import get_logger

logger = get_logger(__name__)

client = OpenAI(api_key=os.getenv("DASHSCOPE_API_KEY"),
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")

KB_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "knowledge")
INDEX_CACHE = os.path.join(os.path.dirname(__file__), "kb_index.pkl")


# ════════════════════════════════════════════════════════════
# embedding（带重试）
# ════════════════════════════════════════════════════════════

def _embed(texts):
    """批量向量化 + 归一化，API 失败自动重试。"""
    out = []
    for b in range(0, len(texts), 10):
        batch = texts[b:b + 10]
        resp = _embed_with_retry(batch)
        for d in resp.data:
            v = np.array(d.embedding, dtype="float32")
            out.append(v / np.linalg.norm(v))
    return np.array(out, dtype="float32")


def _embed_with_retry(batch):
    """单批 embedding 调用，失败重试（指数退避）。"""
    last_err = None
    for attempt in range(settings.api_max_retries):
        try:
            return client.embeddings.create(
                model=settings.embed_model, input=batch,
                dimensions=settings.embed_dim, encoding_format="float")
        except Exception as e:
            last_err = e
            wait = settings.api_retry_delay * (2 ** attempt)  # 2,4,8...
            logger.warning(f"embedding 第{attempt+1}次失败: {e}，{wait}秒后重试")
            time.sleep(wait)
    logger.error(f"embedding 重试{settings.api_max_retries}次仍失败")
    raise last_err


# ════════════════════════════════════════════════════════════
# 文档切分
# ════════════════════════════════════════════════════════════

def _load_and_chunk():
    """读取知识库所有 txt，按 settings 配置切分成带来源的 chunk。"""
    chunks, sources = [], []
    files = sorted(glob.glob(os.path.join(KB_DIR, "*.txt")))
    if not files:
        logger.error(f"知识库目录 {KB_DIR} 下没有 txt 文件")
        return chunks, sources

    size, overlap = settings.chunk_size, settings.chunk_overlap
    for path in files:
        fname = os.path.basename(path)
        with open(path, encoding="utf-8") as f:
            text = f.read().replace("\n", "")
        start = 0
        while start < len(text):
            piece = text[start:start + size]
            if piece.strip():
                chunks.append(piece)
                sources.append(fname)
            start += size - overlap
    logger.info(f"切分完成：{len(chunks)} 个 chunk，来自 {len(files)} 篇文档")
    return chunks, sources


# ════════════════════════════════════════════════════════════
# 索引构建 / 加载（带缓存 + 配置校验）
# ════════════════════════════════════════════════════════════

_index = None
_chunks = None
_sources = None


def _ensure_index(rebuild=False):
    """三层加载：内存 → 磁盘缓存 → 重新构建。
    缓存里记录了 chunk 配置，若 settings 变化则自动失效旧缓存。"""
    global _index, _chunks, _sources

    if _index is not None and not rebuild:
        return

    # 磁盘缓存（校验 chunk 配置是否一致）
    if os.path.exists(INDEX_CACHE) and not rebuild:
        try:
            with open(INDEX_CACHE, "rb") as f:
                data = pickle.load(f)
            cached_cfg = data.get("config", {})
            if (cached_cfg.get("chunk_size") == settings.chunk_size and
                    cached_cfg.get("chunk_overlap") == settings.chunk_overlap and
                    cached_cfg.get("embed_dim") == settings.embed_dim):
                _chunks, _sources = data["chunks"], data["sources"]
                _index = faiss.deserialize_index(data["index"])
                logger.info(f"从缓存加载索引：{len(_chunks)} 个 chunk")
                return
            logger.info("chunk 配置已变化，缓存失效，将重建索引")
        except Exception as e:
            logger.warning(f"缓存加载失败 ({e})，将重建索引")

    # 重新构建
    logger.info("开始构建知识库索引（调用 embedding API）…")
    _chunks, _sources = _load_and_chunk()
    if not _chunks:
        raise RuntimeError("知识库为空，无法构建索引")
    vecs = _embed(_chunks)
    _index = faiss.IndexFlatIP(settings.embed_dim)
    _index.add(vecs)
    with open(INDEX_CACHE, "wb") as f:
        pickle.dump({
            "chunks": _chunks, "sources": _sources,
            "index": faiss.serialize_index(_index),
            "config": {                       # 记录配置用于缓存校验
                "chunk_size": settings.chunk_size,
                "chunk_overlap": settings.chunk_overlap,
                "embed_dim": settings.embed_dim,
            }
        }, f)
    logger.info(f"索引构建完成并已缓存：{len(_chunks)} 个 chunk")


# ════════════════════════════════════════════════════════════
# 工具：检索知识库（置信度过滤 + 去重）
# ════════════════════════════════════════════════════════════

import functools


@functools.lru_cache(maxsize=128)
def _embed_query_cached(query: str):
    """查询向量缓存：相同问题不重复调 embedding API（响应时效优化）。
    诊断中多个Agent常检索相似问题，缓存命中可省去网络往返。"""
    q = "为这个句子生成表示以用于检索相关文章：" + query
    return _embed([q])


def search_knowledge_base(query: str, top_k: int = None, min_score: float = None) -> str:
    """检索企业知识库（SOP、历史案例、工艺/保养规范），返回最相关内容及来源。

    Args:
        query:     检索问题
        top_k:     返回条数，默认取 settings.top_k
        min_score: 最低相关度阈值，低于此值的结果丢弃，默认取 settings.min_score
    """
    if not query or not query.strip():
        return "错误：检索问题不能为空。"

    top_k = top_k or settings.top_k
    min_score = min_score if min_score is not None else settings.min_score

    try:
        _ensure_index()
    except Exception as e:
        logger.error(f"索引构建失败: {e}")
        return "知识库索引构建失败（错误码：KB-001），请联系系统管理员。"

    # 多召回一些候选，给去重和过滤留余地
    pool = min(top_k * 3, len(_chunks))
    try:
        qv = _embed_query_cached(query)   # 带缓存的查询向量化
    except Exception:
        return "知识库检索失败（embedding 服务异常，错误码：KB-002），请稍后重试。"

    scores, ids = _index.search(qv, pool)

    # 去重：同一来源文件只保留相关度最高的一条
    seen_source = {}
    for i, s in zip(ids[0], scores[0]):
        if s < min_score:           # 置信度过滤
            continue
        src = _sources[i]
        if src not in seen_source or s > seen_source[src][1]:
            seen_source[src] = (i, s)

    # 按相关度排序，取 top_k
    ranked = sorted(seen_source.values(), key=lambda x: -x[1])[:top_k]

    if not ranked:
        return (f"知识库中未找到与「{query}」相关度足够高的内容"
                f"（相关度阈值 {min_score}）。建议换用更具体的关键词重新检索。")

    lines = [f"知识库检索结果（问题：{query}，命中 {len(ranked)} 条）："]
    for rank, (i, s) in enumerate(ranked, 1):
        lines.append(f"  [{rank}] 来源《{_sources[i]}》(相关度{s:.3f})：{_chunks[i]}")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════
# 本地自测
# ════════════════════════════════════════════════════════════
if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not os.getenv("DASHSCOPE_API_KEY"):
        raise SystemExit("请先设置 DASHSCOPE_API_KEY 环境变量")

    print("构建/加载知识库索引…")
    _ensure_index()
    print(f"索引就绪：共 {len(_chunks)} 个知识块，来自 {len(set(_sources))} 篇文档。\n")

    for q in [
        "刻蚀机良率下降、颗粒污染，有没有类似的历史处理案例？",
        "良率异常应该按什么步骤排查？",
        "颗粒计数超过多少需要立即停机？",
        "今天午饭吃什么？",   # 故意问无关问题，测试置信度过滤
    ]:
        print("=" * 60)
        print(search_knowledge_base(q, top_k=2))
        print()
