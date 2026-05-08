import { CONFIG } from "../config.js";
import { apiPost } from "./http.js";

/**
 * 导出成片 / 任务入队（按你后端约定调整 body）
 * @param {{ resolution: string, clipIds: string[] }} payload
 */
export async function requestExportVideo(payload) {
    if (CONFIG.USE_MOCK_API) {
        await new Promise((r) => setTimeout(r, 400));
        return { jobId: "mock-" + Date.now(), status: "queued" };
    }
    return apiPost("/api/splice/export", payload);
}
