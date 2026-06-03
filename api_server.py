# -*- coding: utf-8 -*-
"""
REST API 服务：把多智能体诊断系统包装成 HTTP 接口
工程化价值（对应 JD「系统集成、确保高可用与扩展性」）：
  - 诊断能力通过标准 RESTful 接口对外提供，可被前端/其他系统调用
  - 输入用 Pydantic 模型校验，非法请求自动返回 422
  - 提供健康检查、设备列表、诊断、工单查询等接口
  - 自动生成交互式 API 文档（Swagger UI）

启动：
  cd Multi_Agent
  uvicorn api_server:app --reload --port 8000
然后浏览器打开 http://127.0.0.1:8000/docs 即可交互测试。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import config  # 自动加载 DASHSCOPE_API_KEY
from settings import settings
from logger_config import get_logger

sys.path.append(os.path.join(os.path.dirname(__file__), "agents"))
sys.path.append(os.path.join(os.path.dirname(__file__), "tools"))

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, Field
from typing import Optional

import db_tools
import action_tools
from main import diagnose
import vision_agent      # 多模态：看故障照片
import toolsmith         # 动态工具：理解需求→设计只读查询工具→执行

logger = get_logger(__name__)

app = FastAPI(
    title="制造企业多智能体诊断系统 API",
    description="基于多Agent协作 + Agentic RAG 的设备异常诊断服务",
    version="2.0.0",
)


# ════════════════════════════════════════════════════════════
# 请求/响应模型（Pydantic 自动校验）
# ════════════════════════════════════════════════════════════

class DiagnoseRequest(BaseModel):
    equipment_id: str = Field(..., description="设备编号，如 EQP-03", examples=["EQP-03"])
    user_request: str = Field(..., min_length=5, description="自然语言描述的问题",
                              examples=["3号机台良率掉到88%了，帮我分析原因并处理"])


class DiagnoseResponse(BaseModel):
    equipment_id: str
    report: str
    status: str = "success"


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str


class AskRequest(BaseModel):
    question: str = Field(..., min_length=2, description="关于工厂数据的自然语言问题（动态Text2SQL回答）",
                          examples=["哪台设备未解决报警最多？", "现在有哪些设备？"])


# ════════════════════════════════════════════════════════════
# 接口
# ════════════════════════════════════════════════════════════

@app.get("/", response_model=HealthResponse, summary="健康检查")
def health():
    """服务健康检查，用于负载均衡/监控探针。"""
    return HealthResponse(status="healthy", service="multi-agent-diagnosis", version="2.0.0")


@app.get("/equipment", summary="查询有效设备列表")
def list_equipment():
    """返回系统中所有有效设备编号。"""
    return {"equipment": sorted(settings.valid_equipment), "count": len(settings.valid_equipment)}


@app.post("/diagnose", response_model=DiagnoseResponse, summary="设备异常诊断")
def run_diagnose(req: DiagnoseRequest):
    """
    核心接口：对指定设备运行完整的多智能体诊断流程。
    会依次/并行调用数据分析、知识检索、质量评审、行动执行等Agent，
    返回结构化诊断报告。注意：单次诊断耗时约60~90秒（多次LLM调用）。
    """
    if req.equipment_id not in settings.valid_equipment:
        raise HTTPException(
            status_code=400,
            detail=f"设备编号 '{req.equipment_id}' 不存在，有效设备：{sorted(settings.valid_equipment)}")

    logger.info(f"收到诊断请求: {req.equipment_id}")
    try:
        report = diagnose(req.user_request, req.equipment_id)
    except Exception as e:
        logger.error(f"诊断失败: {e}")
        raise HTTPException(status_code=500, detail=f"诊断过程异常: {e}")

    return DiagnoseResponse(equipment_id=req.equipment_id, report=report)


@app.get("/equipment/{equipment_id}/yield", summary="查询设备良率趋势")
def get_yield(equipment_id: str, days: int = 7):
    """快速查询某设备良率趋势（不走完整诊断，直接返回数据）。"""
    if equipment_id not in settings.valid_equipment:
        raise HTTPException(status_code=400, detail=f"设备 '{equipment_id}' 不存在")
    return {"equipment_id": equipment_id, "result": db_tools.query_yield_trend(equipment_id, days)}


@app.get("/work_orders", summary="查询工单列表")
def get_work_orders(equipment_id: Optional[str] = None, status: Optional[str] = None):
    """查询工单，可按设备和状态过滤。"""
    result = action_tools.list_work_orders(equipment_id, status)
    return {"result": result}


@app.get("/work_orders/statistics", summary="工单统计概览")
def work_order_stats():
    """返回工单状态分布和设备分布统计。"""
    return {"result": action_tools.work_order_statistics()}


@app.post("/diagnose_image", summary="拍照诊断（多模态）")
async def diagnose_image(
    file: UploadFile = File(..., description="设备/晶圆故障照片"),
    equipment_id: Optional[str] = Form(None, description="设备编号；填了就接着跑完整诊断"),
    note: str = Form("", description="补充说明，如'3号刻蚀机的晶圆'"),
):
    """上传故障照片 → 视觉Agent转成结构化观察；若给了设备号，再把观察接入诊断流水线
    （结合数据库客观数据交叉印证）输出完整报告。"""
    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="上传的图片为空。")
    try:
        observation = vision_agent.inspect_image(
            image_bytes, file.content_type or "image/jpeg", note)
    except Exception as e:
        logger.error(f"视觉检测失败: {e}")
        raise HTTPException(status_code=500, detail=f"视觉检测异常: {e}")

    result = {"observation": observation}
    if equipment_id:
        if equipment_id not in settings.valid_equipment:
            raise HTTPException(status_code=400, detail=f"设备编号 '{equipment_id}' 不存在。")
        req = (f"{note}\n" if note else "") + f"（视觉检测观察）：{observation}"
        try:
            result["report"] = diagnose(req, equipment_id)
        except Exception as e:
            logger.error(f"图像诊断失败: {e}")
            raise HTTPException(status_code=500, detail=f"诊断过程异常: {e}")
    return result


@app.post("/ask", summary="自然语言问数据（动态Text2SQL）")
def ask(req: AskRequest):
    """把没有现成工具覆盖的数据问题交给"工具设计师Agent"：
    理解需求→设计只读查询工具→只读沙箱执行→中文作答。"""
    return {"question": req.question, "answer": toolsmith.serve(req.question)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
