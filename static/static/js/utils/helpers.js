export function escHtml(s) {
    return String(s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/\n/g, "<br>");
}

/** 属性内使用（如 src、href） */
export function escAttr(s) {
    return String(s)
        .replace(/&/g, "&amp;")
        .replace(/"/g, "&quot;")
        .replace(/</g, "&lt;");
}

export function formatSize(bytes) {
    if (bytes > 1e9) return (bytes / 1e9).toFixed(1) + "GB";
    if (bytes > 1e6) return (bytes / 1e6).toFixed(0) + "MB";
    return (bytes / 1e3).toFixed(0) + "KB";
}

export function parseDur(s) {
    if (!s) return 10;
    const parts = String(s).split(":");
    if (parts.length === 2) return parseInt(parts[0], 10) * 60 + parseInt(parts[1], 10);
    return parseInt(s, 10) || 10;
}

export function formatDuration(secs) {
    const m = Math.floor(secs / 60),
        s = secs % 60;
    return m + ":" + String(s).padStart(2, "0");
}
