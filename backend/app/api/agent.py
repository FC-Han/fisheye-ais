"""智能体接口。"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..agent import agent as agent_mod

router = APIRouter(prefix="/api/agent", tags=["agent"])


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    history: list[ChatMessage] = Field(default_factory=list)


@router.post("/chat")
def chat(req: ChatRequest):
    return agent_mod.chat(req.message, [h.model_dump() for h in req.history])


@router.get("/tools")
def list_tools():
    from ..agent.tools import TOOL_SCHEMAS

    return {"tools": TOOL_SCHEMAS}
