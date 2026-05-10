import { CONFIG } from "../config.js";
import { apiDelete, apiGet, apiPost, apiRequest } from "./http.js";

/** GET /api/projects/:id/assets；失败时抛出，由调用方提示（不再静默返回 null 导致永远不合并素材） */
export async function fetchProjectAssets(projectId, signal) {
    if (CONFIG.USE_MOCK_API) return [];
    const path = `/api/projects/${encodeURIComponent(projectId)}/assets`;
    const data = await apiGet(path, signal ? { signal } : {});
    return Array.isArray(data) ? data : [];
}

/** GET /api/projects/:id/storyboard/runs — 本项目 storyboard_runs 目录列表（成片下拉） */
export async function fetchStoryboardRuns(projectId, signal) {
    if (CONFIG.USE_MOCK_API) return [];
    const path = `/api/projects/${encodeURIComponent(projectId)}/storyboard/runs`;
    const data = await apiGet(path, signal ? { signal } : {});
    return Array.isArray(data) ? data : [];
}

/** 后端就绪后：GET /api/projects */
export async function fetchProjects() {
    if (CONFIG.USE_MOCK_API) return null;
    return apiGet("/api/projects");
}

/** POST /api/projects */
export async function createProjectRemote(body) {
    if (CONFIG.USE_MOCK_API) return null;
    return apiPost("/api/projects", body);
}

/** POST multipart /api/projects/:id/assets */
export async function uploadAssetRemote(projectId, file) {
    if (CONFIG.USE_MOCK_API) return null;
    const fd = new FormData();
    fd.append("file", file);
    const path = `/api/projects/${encodeURIComponent(projectId)}/assets`;
    const base = CONFIG.API_BASE_URL.replace(/\/$/, "");
    const res = await fetch(base + path, { method: "POST", body: fd });
    if (!res.ok) {
        let detail = res.statusText || "upload failed";
        try {
            const t = await res.text();
            if (t) {
                try {
                    const j = JSON.parse(t);
                    if (j && j.detail != null) {
                        detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
                    } else {
                        detail = t.slice(0, 200);
                    }
                } catch {
                    detail = t.slice(0, 200);
                }
            }
        } catch {
            /* ignore */
        }
        throw new Error(detail);
    }
    return res.json();
}

/** DELETE /api/projects/:id */
export async function deleteProjectRemote(projectId) {
    if (CONFIG.USE_MOCK_API) return;
    const path = `/api/projects/${encodeURIComponent(projectId)}`;
    await apiRequest(path, { method: "DELETE" });
}

/** DELETE /api/projects/:projectId/assets/:assetId — 删除本项目名下一条已同步素材（含磁盘文件） */
export async function deleteProjectAssetRemote(projectId, assetId) {
    if (CONFIG.USE_MOCK_API) return;
    const path = `/api/projects/${encodeURIComponent(projectId)}/assets/${encodeURIComponent(String(assetId))}`;
    await apiDelete(path);
}
