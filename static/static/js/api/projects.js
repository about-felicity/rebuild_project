import { CONFIG } from "../config.js";
import { apiGet, apiPost, apiRequest } from "./http.js";

/** GET /api/projects/:id/assets；失败时抛出，由调用方提示（不再静默返回 null 导致永远不合并素材） */
export async function fetchProjectAssets(projectId, signal) {
    if (CONFIG.USE_MOCK_API) return [];
    const path = `/api/projects/${encodeURIComponent(projectId)}/assets`;
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
    if (!res.ok) throw new Error("upload failed");
    return res.json();
}

/** DELETE /api/projects/:id */
export async function deleteProjectRemote(projectId) {
    if (CONFIG.USE_MOCK_API) return;
    const path = `/api/projects/${encodeURIComponent(projectId)}`;
    await apiRequest(path, { method: "DELETE" });
}
