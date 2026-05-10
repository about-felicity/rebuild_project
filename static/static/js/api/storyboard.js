import { CONFIG } from "../config.js";

/**
 * POST multipart + SSE：分镜流水线进度事件。
 * @param {string} projectId
 * @param {FormData} formData
 * @param {(obj: Record<string, unknown>) => void} onEvent
 * @param {AbortSignal} [signal]
 */
export async function streamStoryboardPipeline(projectId, formData, onEvent, signal) {
    const base = CONFIG.API_BASE_URL.replace(/\/$/, "");
    const path = `/api/projects/${encodeURIComponent(projectId)}/storyboard/stream`;
    const res = await fetch(base + path, {
        method: "POST",
        body: formData,
        signal,
    });
    if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(`HTTP ${res.status}: ${text.slice(0, 400)}`);
    }
    const reader = res.body?.getReader();
    if (!reader) throw new Error("响应无 body");
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
            const chunk = buf.slice(0, idx);
            buf = buf.slice(idx + 2);
            const lines = chunk.split("\n");
            for (const line of lines) {
                if (line.startsWith("data: ")) {
                    const raw = line.slice(6).trim();
                    try {
                        const obj = JSON.parse(raw);
                        onEvent(obj);
                    } catch {
                        /* ignore */
                    }
                }
            }
        }
    }
    if (buf.trim()) {
        for (const line of buf.split("\n")) {
            if (line.startsWith("data: ")) {
                const raw = line.slice(6).trim();
                try {
                    const obj = JSON.parse(raw);
                    onEvent(obj);
                } catch {
                    /* ignore */
                }
            }
        }
    }
}

/**
 * POST form + SSE：分镜逐镜图生视频 → 拼接成片 → 视频库。
 * @param {string} projectId
 * @param {string} runId  storyboard_runs 子目录名（与「分镜流水线」完成时返回的 run_id 一致）
 * @param {(obj: Record<string, unknown>) => void} onEvent
 * @param {AbortSignal} [signal]
 */
export async function streamStoryboardVideoPipeline(projectId, runId, onEvent, signal) {
    const base = CONFIG.API_BASE_URL.replace(/\/$/, "");
    const path = `/api/projects/${encodeURIComponent(projectId)}/storyboard/video/stream`;
    const fd = new FormData();
    fd.append("run_id", String(runId || "").trim());
    const res = await fetch(base + path, {
        method: "POST",
        body: fd,
        signal,
    });
    if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(`HTTP ${res.status}: ${text.slice(0, 400)}`);
    }
    const reader = res.body?.getReader();
    if (!reader) throw new Error("响应无 body");
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
            const chunk = buf.slice(0, idx);
            buf = buf.slice(idx + 2);
            const lines = chunk.split("\n");
            for (const line of lines) {
                if (line.startsWith("data: ")) {
                    const raw = line.slice(6).trim();
                    try {
                        const obj = JSON.parse(raw);
                        onEvent(obj);
                    } catch {
                        /* ignore */
                    }
                }
            }
        }
    }
    if (buf.trim()) {
        for (const line of buf.split("\n")) {
            if (line.startsWith("data: ")) {
                const raw = line.slice(6).trim();
                try {
                    const obj = JSON.parse(raw);
                    onEvent(obj);
                } catch {
                    /* ignore */
                }
            }
        }
    }
}
