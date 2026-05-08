"""Anthropic Messages API：``system`` 提示词与 ``tools`` JSON Schema 定义。"""

from __future__ import annotations

from typing import Any

# 系统角色说明：约束模型如何选工具、禁止伪造 project_id、视频必填参数等。
SYSTEM = """
你是一个分镜与视频创作 Agent。

当前会话已经绑定 project_id（由服务端通过上下文变量注入给工具逻辑）；
禁止在工具参数里伪造项目 id，也不要在回复中假装切换项目。

工具选用规则：
1. 分镜格、分镜条、镜头板、storyboard 画面 → 仅调用 generate_storyboard_image。
2. 角色立绘、人设图、产品静物、商品素材 → 仅调用 generate_asset_image；
   destination 只能是 character_library（角色）或 product_library（产品），不得使用其它取值。
3. 用户明确要「短视频 / 图生视频 / 动起来的镜头」→ 仅调用 generate_video_clip；
   调用前必须向用户确认 duration（正整数秒）与 source_image（可公网访问的 URL 或 data:image）；
   任一缺失则先追问，禁止猜测或留空。

不要混用工具用途；参数不齐时不要强行调用视频工具。

当用户已经明确库类型（例如「角色库」「分镜库」「产品库」或同意你的二选一）时：
**下一条必须调用**对应的 generate_asset_image / generate_storyboard_image，**禁止**仅用文字声称「已生成」「生成成功」而不调用工具。
若用户本轮只打了「角色库」等短句，服务端会把**上一条用户长需求**一并塞进本条消息；你必须用其中的画面描述作为 user_query 调工具。

回复风格（强制执行）：
- 生图/生视频工具**只要成功**，服务端会**丢弃你的长回复**，只向用户展示固定一句「已保存到某库。」你仍应正常调用工具。
- 因此工具成功后你可**不写最终段落**或只写一个字，但**禁止**写：标题/#标题、--- 分隔线、✅🎉 等 emoji、「生成成功」「真实的…」「已生成并存入」等营销句、
  「后续可以」「下一步」类列表、任何链接、Markdown 图片语法、描写画面超过一行。
- 工具**失败**或未调用工具时：用简短中文说明原因或追问，同样不要 emoji 与小作文。
"""

# tools：名称须与 tool_dispatch.collect_tool_results 分支完全一致。
TOOLS: list[dict[str, Any]] = [
    {
        "name": "generate_storyboard_image",
        "description": "用户要分镜图、分镜条、storyboard 视觉稿时使用；结果进入分镜库。",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_query": {"type": "string", "description": "画面/镜头描述"},
                "asset_name": {"type": "string", "description": "短标题，可空"},
                "style_hint": {"type": "string"},
                "aspect_ratio": {"type": "string", "default": "16:9"},
                "n": {"type": "integer", "default": 1},
            },
            "required": ["user_query"],
        },
    },
    {
        "name": "generate_asset_image",
        "description": "角色图、产品静帧、商业素材图；destination 仅限角色库或产品库。",
        "input_schema": {
            "type": "object",
            "properties": {
                "destination": {
                    "type": "string",
                    "enum": ["character_library", "product_library"],
                },
                "user_query": {"type": "string"},
                "asset_name": {"type": "string"},
                "style_hint": {"type": "string"},
                "aspect_ratio": {"type": "string", "default": "16:9"},
                "n": {"type": "integer", "default": 1},
            },
            "required": ["destination", "user_query"],
        },
    },
    {
        "name": "generate_video_clip",
        "description": "图生视频；必须先有合法的 duration（秒）与 source_image。",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "source_image": {
                    "type": "string",
                    "description": "首帧 URL 或 data:image",
                },
                "duration": {"type": "integer", "description": "秒数，>0"},
                "camera_move": {"type": "string", "default": "static"},
                "video_title": {"type": "string"},
            },
            "required": ["prompt", "source_image", "duration"],
        },
    },
]
