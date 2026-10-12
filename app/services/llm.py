"""生成层，三档可换。

rule  不调模型（默认），建议由规则拼
local 本机 Ollama，数据不出本机
api   远端 OpenAI 兼容接口，自己配 key

模型只负责把建议说得自然点，排序结果它碰不到。
所以模型挂了也不能让分诊失败，调用方拿 try/except 兜住了。
"""
from __future__ import annotations

import httpx

from app.core.config import Settings

PROMPT = """你是一名医院导诊台护士。患者主诉：{symptom}
系统初步判断可能相关的科室：{departments}

请用一句话（不超过 40 字）告诉患者接下来该怎么做，语气要平和、专业。
注意：不要给出诊断结论，不要推荐药品，只做就医指引。"""


def build_advice(matches: list[tuple[str, str]]) -> str:
    """rule 模式下的兜底话术，零依赖。"""
    if not matches:
        return "建议到导诊台由护士协助分诊。"
    top = matches[0]
    return f"建议优先挂{top[1]}，就诊前可先到导诊台确认。"


def generate_advice(symptom: str, department_names: list[str], settings: Settings) -> str:
    prompt = PROMPT.format(symptom=symptom, departments="、".join(department_names))

    if settings.triage_mode == "local":
        resp = httpx.post(
            f"{settings.ollama_base_url}/api/generate",
            json={"model": settings.ollama_model, "prompt": prompt, "stream": False},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()

    if settings.triage_mode == "api":
        if not settings.api_key:
            raise RuntimeError("APP_API_KEY 未配置")
        resp = httpx.post(
            f"{settings.api_base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.api_key}"},
            json={
                "model": settings.api_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()

    return build_advice([(n, n) for n in department_names])
