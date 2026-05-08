import { apiPost } from "./http.js";

/**
 * @param {{ project_id: string, share_text: string, video_title?: string }} body
 */
export function requestDouyinFetch(body) {
    return apiPost("/api/douyin/fetch", body);
}
