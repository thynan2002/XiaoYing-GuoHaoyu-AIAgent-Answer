"""全局配置与数据加载（费用表 / 素材库）。"""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DEFAULT_PLAN_DIR = DATA_DIR / "saved_plans"

# 硬上限：防止模型在失败循环里无限重试
DEFAULT_MAX_STEPS = 12
DEFAULT_MAX_TOOL_CALLS = 12
# 同一工具连续失败后的最大原地重试次数（超过则必须换方案或收尾）
MAX_SAME_TOOL_RETRY = 1

# 真实模型接入（OpenAI 兼容协议）；不配置时用离线 mock 规划器
LLM_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
LLM_API_KEY = os.environ.get("OPENAI_API_KEY", "")
LLM_TIMEOUT_S = float(os.environ.get("OPENAI_TIMEOUT_S", "45"))


def load_costs() -> dict:
    with open(DATA_DIR / "costs.json", encoding="utf-8") as f:
        return json.load(f)


def load_materials() -> dict:
    with open(DATA_DIR / "materials.json", encoding="utf-8") as f:
        return json.load(f)
