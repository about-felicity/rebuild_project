import { CONFIG } from "../config.js";
import { apiDelete, apiGet, apiRequest } from "./http.js";

/** @param {unknown} data */
function normalizeMessagesResponse(data) {
    if (data == null) return [];
    let obj = data;
    if (typeof data === "string") {
        try {
            obj = JSON.parse(data);
        } catch {
            return [];
        }
    }
    if (typeof obj !== "object" || obj === null) return [];
    const raw = /** @type {Record<string, unknown>} */ (obj);
    const arr = raw.messages ?? raw.message;
    return Array.isArray(arr) ? arr : [];
}

/**
 * GET /api/agent/chat-project-ids — 库中有会话的 project_id，最近活跃在前（短超时，避免卡住整页 init）
 * @returns {Promise<string[]>}
 */
export async function fetchChatProjectIds() {
    if (CONFIG.USE_MOCK_API) return [];
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 8000);
    try {
        const data = await apiGet("/api/agent/chat-project-ids", { signal: ctrl.signal });
        if (data == null || typeof data !== "object") return [];
        const ids = /** @type {Record<string, unknown>} */ (data).project_ids;
        return Array.isArray(ids) ? ids.map((x) => String(x)) : [];
    } catch (_) {
        return [];
    } finally {
        clearTimeout(t);
    }
}

/**
 * GET /api/agent/sessions/:projectId/messages
 * @param {string} projectId
 * @param {AbortSignal} [signal]
 * @returns {Promise<{ messages: Array<{ role: string, content: string }> }>}
 */

export async function fetchAgentMessages(projectId, signal) {
    if (CONFIG.USE_MOCK_API) {
        return { messages: [] };
    }
    const boot =
        Number.isFinite(Number(CONFIG.BOOTSTRAP_READ_TIMEOUT_MS)) &&
        Number(CONFIG.BOOTSTRAP_READ_TIMEOUT_MS) > 0
            ? Number(CONFIG.BOOTSTRAP_READ_TIMEOUT_MS)
            : 18000;
    const data = await apiGet(
        `/api/agent/sessions/${encodeURIComponent(projectId)}/messages`,
        {
            ...(signal ? { signal } : {}),
            timeoutMs: boot,
        },
    );
    const messages = normalizeMessagesResponse(data);
    return { messages };
}

function mockAgentReply(text, projectName) {
    const responses = {
        default: [
            "已收到你的需求。基于当前项目素材，我建议使用慢动作切换配合环境音效，能有效增强沉浸感。\n\n我可以为你生成具体的分镜脚本或提示词，请告诉我目标平台（如 Sora、Runway、剪映）。",
            "这是个很好的创意方向！从素材分析来看，你的素材色调偏暖，建议搭配同色系 BGM 和字幕设计，整体风格会更统一。\n\n需要我生成完整的视频制作方案吗？",
            "根据你描述的场景，推荐以下分镜结构：\n1. 广角建立镜头（5s）\n2. 中景人物/主体（8s）\n3. 特写细节（3s）\n4. 回归广角收尾（5s）\n\n每个分镜我都可以生成对应的 AI 提示词。",
            '明白了。这类内容在 Sora 平台表现最佳，建议提示词中包含：\n· 具体的光线描述（如"golden hour backlight"）\n· 镜头运动描述（如"slow dolly in"）\n· 情绪关键词（如"cinematic, nostalgic"）\n\n要我生成完整提示词吗？',
        ],
        分析当前项目的素材风格: `正在分析「${projectName || "当前项目"}」素材...\n\n素材风格分析报告：\n✓ 色调：暖色系为主，金黄色+橙色占70%\n✓ 拍摄手法：以固定机位延时为主，少量手持\n✓ 分辨率：4K高质量，适合商业输出\n✓ 时长：平均单段时长约1.5分钟\n\n建议剪辑风格：电影感慢节奏，配合管弦乐BGM`,
        帮我生成一段30秒的短片脚本:
            "30秒短片脚本（城市夜景主题）：\n\n[00:00-00:05] 建立镜头\n城市夜空全景，霓虹灯逐渐亮起\n\n[00:05-00:12] 主体推进\n慢推镜头，聚焦繁忙的十字路口\n\n[00:12-00:22] 细节刻画\n交替剪辑：车灯轨迹、行人剪影、招牌倒影\n\n[00:22-00:28] 情绪升华\n延时加速，城市生命力爆发\n\n[00:28-00:30] 收尾定格\n回归城市轮廓，文字标题渐显",
        推荐适合婚礼纪录片的运镜方式:
            "婚礼纪录片运镜推荐：\n\n📷 仪式阶段\n· 长焦守候拍摄（不打扰主角）\n· 缓慢横移跟随步伐\n· 适当仰拍表达庄重感\n\n🎊 庆典阶段\n· 环绕移动增加动感\n· 手持轻微抖动体现真实感\n· 大景深虚化烘托氛围\n\n💑 人像阶段\n· 浅景深突出主体\n· 侧光+逆光增加情感厚度\n· 缓慢推进表达情绪",
    };
    if (responses[text]) return responses[text];
    const pool = responses.default;
    return pool[Math.floor(Math.random() * pool.length)];
}

/**
 * @param {{ text: string, projectName?: string, projectId: string, agent_mode?: "normal"|"abstract", referenced_asset_ids?: number[] }} payload
 * @returns {Promise<{ reply: string, tool_trace?: Record<string, unknown> | null }>}
 */
export async function requestAgentReply(payload) {
    if (CONFIG.USE_MOCK_API) {
        const delay = 400 + Math.random() * 500;
        await new Promise((r) => setTimeout(r, delay));
        const reply = mockAgentReply(payload.text, payload.projectName || "");
        return { reply, tool_trace: null };
    }
    const mode = payload.agent_mode === "abstract" ? "abstract" : "normal";
    const body = {
        message: payload.text,
        project: payload.projectName,
        project_id: payload.projectId,
        agent_mode: mode,
    };
    const rids = Array.isArray(payload.referenced_asset_ids)
        ? payload.referenced_asset_ids
              .map((x) => Number(x))
              .filter((n) => Number.isFinite(n) && n > 0)
        : [];
    if (rids.length) body.referenced_asset_ids = rids;
    const agentMs = Number(CONFIG.AGENT_CHAT_TIMEOUT_MS);
    const chatMs =
        Number.isFinite(agentMs) && agentMs > 0 ? agentMs : 300000;
    const data = await apiRequest("/api/agent/chat", {
        method: "POST",
        body: JSON.stringify(body),
        timeoutMs: chatMs,
    });
    if (typeof data === "string") return { reply: data, tool_trace: null };
    const reply = data.reply ?? data.content ?? data.message ?? JSON.stringify(data);
    return { reply: String(reply), tool_trace: data.tool_trace ?? null };
}

/**
 * 清除服务端该项目的 Agent 上下文（与 SESSIONS[project_id] 对齐）。
 * Mock 模式下不调接口。
 * @param {string} projectId
 */
export async function clearAgentSession(projectId) {
    if (CONFIG.USE_MOCK_API) return;
    const path = `/api/agent/sessions/${encodeURIComponent(projectId)}`;
    await apiDelete(path);
}
