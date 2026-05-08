/**
 * 入口：挂载 UI 并把回调挂到 window（兼容 HTML 内联 onclick）。
 * 联调后端：改 config.js 的 API_BASE_URL / USE_MOCK_API；在 api/*.js 中对齐路由与字段。
 */
import { mountFrameOS } from "./app/workflow-ui.js";

mountFrameOS();
