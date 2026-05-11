/**
 * 环境配置：联调时改这里即可，勿在业务组件里写死域名。
 *
 * - 推荐：只开 uvicorn，打开 http://主机:8000/app/（与 API 同域，无 CORS）。
 *   本机用 127.0.0.1；局域网用手机/其它电脑时用你电脑的 IPv4（需 serve-lan.bat）。
 * - 若用 python -m http.server 8080 打开静态页，会自动回退请求 http://127.0.0.1:8000。
 */
function resolveApiBaseUrl() {
    if (typeof window === "undefined" || !window.location) {
        return "http://127.0.0.1:8000";
    }
    const { port, pathname } = window.location;
    if (port === "8000" && pathname.startsWith("/app")) {
        return "";
    }
    return "http://127.0.0.1:8000";
}

export const CONFIG = {
    /** API 根；'' 表示与当前页面同主机同端口（仅适用于 8000/app） */
    API_BASE_URL: resolveApiBaseUrl(),
    /**
     * true：Agent 等接口走本地 mock，不调后端。
     * false：联调真实 API；需与后端 CORS / 路径一致，且 `project_id` 会话与后端 SESSIONS 对齐。
     */
    USE_MOCK_API: false,
    /** 全局 HTTP 默认超时 ms（非 Agent 的短请求也会用；不宜过小） */
    REQUEST_TIMEOUT_MS: 120000,
    /** Agent /api/agent/chat 单轮超时（含图生视频任务轮询，须 ≥ 服务端等待） */
    AGENT_CHAT_TIMEOUT_MS: 300000,
    /**
     * 首屏/切换项目时拉会话与素材列表的超时（须明显短于 REQUEST_TIMEOUT_MS），
     * 避免后端不可达时整页卡在「加载」态两分钟。
     */
    BOOTSTRAP_READ_TIMEOUT_MS: 18000,
};
