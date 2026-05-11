import { CONFIG } from "../config.js";

function buildUrl(path) {
    const base = CONFIG.API_BASE_URL.replace(/\/$/, "");
    const p = path.startsWith("/") ? path : "/" + path;
    return base + p;
}

/**
 * @param {string} path 如 /api/agent/chat
 * @param {RequestInit} options
 */
export async function apiRequest(path, options = {}) {
    const ctrl = new AbortController();
    const {
        signal: externalSignal,
        headers: optHeaders = {},
        timeoutMs: timeoutMsOpt,
        ...rest
    } = options;
    if (externalSignal) {
        if (externalSignal.aborted) {
            ctrl.abort();
        } else {
            externalSignal.addEventListener("abort", () => ctrl.abort(), { once: true });
        }
    }
    const cap =
        Number.isFinite(Number(timeoutMsOpt)) && Number(timeoutMsOpt) > 0
            ? Number(timeoutMsOpt)
            : CONFIG.REQUEST_TIMEOUT_MS;
    const t = setTimeout(() => ctrl.abort(), cap);
    try {
        const headers = { ...optHeaders };
        const method = (rest.method || "GET").toUpperCase();
        if (
            ["POST", "PUT", "PATCH"].includes(method) &&
            typeof rest.body === "string" &&
            !headers["Content-Type"]
        ) {
            headers["Content-Type"] = "application/json";
        }
        const res = await fetch(buildUrl(path), {
            ...rest,
            signal: ctrl.signal,
            headers,
        });
        if (!res.ok) {
            const text = await res.text().catch(() => "");
            throw new Error(`HTTP ${res.status} ${res.statusText}${text ? ": " + text.slice(0, 200) : ""}`);
        }
        const ct = (res.headers.get("content-type") || "").toLowerCase();
        if (ct.includes("application/json")) return res.json();
        const text = await res.text();
        const t = text.trim();
        if (t.startsWith("{") || t.startsWith("[")) {
            try {
                return JSON.parse(text);
            } catch {
                /* fallthrough */
            }
        }
        return text;
    } finally {
        clearTimeout(t);
    }
}

/**
 * @param {string} path
 * @param {Pick<RequestInit, "signal">} [init]
 */
export function apiGet(path, init = {}) {
    return apiRequest(path, { method: "GET", ...init });
}

export function apiPost(path, body) {
    return apiRequest(path, {
        method: "POST",
        body: JSON.stringify(body ?? {}),
    });
}

/** @param {string} path */
export function apiDelete(path) {
    return apiRequest(path, { method: "DELETE" });
}
