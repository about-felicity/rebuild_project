/**
 * 环境配置：联调时改这里即可，勿在业务组件里写死域名。
 * 开发：Vite/webpack dev server 可配代理，BASE_URL 仍用 ''，由代理转发 /api。
 */
export const CONFIG = {
    /** API 根路径，如 https://api.example.com 或 ''（同域） */
    API_BASE_URL: "http://127.0.0.1:8000",
    /**
     * true：Agent 等接口走本地 mock，不调后端。
     * false：联调真实 API；需与后端 CORS / 路径一致，且 `project_id` 会话与后端 SESSIONS 对齐。
     */
    USE_MOCK_API: false,
    /** 请求超时 ms（Agent 多轮可能较长，联调可改为 120000+） */
    REQUEST_TIMEOUT_MS: 120000,
};
