import { state } from "../state/store.js";
import { CONFIG } from "../config.js";
import {
    clearAgentSession,
    fetchAgentMessages,
    fetchChatProjectIds,
    requestAgentReply,
} from "../api/agent.js";
import { deleteProjectRemote, fetchProjectAssets } from "../api/projects.js";
import { requestStoryboardJson } from "../api/analysis.js";
import { requestDouyinFetch } from "../api/douyin.js";
import { requestExportVideo } from "../api/splice.js";
import { escHtml, escAttr, formatSize, parseDur, formatDuration } from "../utils/helpers.js";

/** 刷新后恢复项目列表与上次选中项，避免聊天记录因 project_id 错位而「看不见」 */
const LS_PROJECTS = "frameos_projects";
const SS_ACTIVE_PROJECT = "frameos_active_project_id";
/** 与 sessionStorage 同源策略不同：localhost / 127.0.0.1 各自一份 session，用 localStorage 备份上次项目 id */
const LS_ACTIVE_PROJECT = "frameos_active_project_id";

/** 与后端约定：video | asset | storyboard；生成物必须带 library 并与 project_id 绑定 */
const MEDIA_LIB_LABEL = { video: "视频库", asset: "素材库", storyboard: "分镜库" };

/** 与 sendChatMessage / renderChatForProject 共用，便于切换项目后还原「正在输入」 */
const CHAT_TYPING_DOM_ID = "active-agent-typing";

function inferMediaLibrary(m) {
    if (!m || typeof m !== "object") return "asset";
    const L = m.library;
    if (L === "video" || L === "asset" || L === "storyboard") return L;
    const t = m.type;
    if (t === "video") return "video";
    if (t === "storyboard") return "storyboard";
    return "asset";
}

function migrateProjectMediaLibraries(project) {
    if (!project) return;
    if (!Array.isArray(project.media)) project.media = [];
    for (const m of project.media) {
        m.library = inferMediaLibrary(m);
    }
}

let currentMediaLibraryFilter = "all";

/** @param {{ media?: unknown[] }} p */
function projectMediaCount(p) {
    return p && Array.isArray(p.media) ? p.media.length : 0;
}

function setTopbarInfo(text) {
    const el = document.getElementById("topbar-info");
    if (el) el.textContent = text;
}

/** 与后端 project_assets.id 区分，避免与本地 Date.now() id 冲突 */
const SERVER_ASSET_ID_BASE = 2_000_000_000;

function mergeServerAssetsIntoProject(project, rows) {
    if (!project || !Array.isArray(rows) || rows.length === 0) return;
    if (!Array.isArray(project.media)) project.media = [];
    const seen = new Set(
        project.media.filter((m) => m._serverAssetId != null).map((m) => Number(m._serverAssetId)),
    );
    for (const r of rows) {
        const sid = Number(r.id);
        if (!Number.isFinite(sid)) continue;

        const lib =
            r.library === "video" || r.library === "asset" || r.library === "storyboard"
                ? r.library
                : "asset";
        const kind = String(r.kind || "").toLowerCase();
        const type =
            kind === "video"
                ? "video"
                : kind === "audio"
                  ? "audio"
                  : kind === "storyboard"
                    ? "storyboard"
                    : "image";
        const meta = r.meta && typeof r.meta === "object" ? r.meta : {};
        const uri = r.uri != null ? String(r.uri) : "";

        if (seen.has(sid)) {
            const hit = project.media.find((m) => Number(m._serverAssetId) === sid);
            if (hit) {
                hit.url = uri;
                hit.name = r.name || hit.name || "未命名";
                hit.library = lib;
                hit.type = type;
                hit.source = "server";
                if (meta.size != null) hit.size = String(meta.size);
                if (meta.dur != null) hit.dur = String(meta.dur);
                if (meta.res != null) hit.res = String(meta.res);
            }
            continue;
        }

        project.media.push({
            id: SERVER_ASSET_ID_BASE + sid,
            _serverAssetId: sid,
            name: r.name || "未命名",
            type,
            library: lib,
            size: meta.size != null ? String(meta.size) : "—",
            dur: meta.dur != null ? String(meta.dur) : undefined,
            res: meta.res != null ? String(meta.res) : undefined,
            url: uri,
            source: "server",
        });
        seen.add(sid);
    }
    migrateProjectMediaLibraries(project);
}

/** Agent 对话成功后拉取服务端「本项素材」并合并进当前项目（后端实现 GET /api/projects/:id/assets 后生效） */
async function refreshServerAssetsForActiveProject() {
    if (!state.activeProject) return;
    await refreshServerAssetsForProject(state.activeProject);
}

/** 按指定项目拉取 assets 并合并（回复可能落在非当前选中项目时仍更新其 media） */
async function refreshServerAssetsForProject(project) {
    if (!project) return;
    const pid = String(project.id);
    try {
        const rows = await fetchProjectAssets(pid);
        const p = state.projects.find((x) => String(x.id) === pid);
        if (!p) return;
        if (!Array.isArray(rows) || rows.length === 0) return;
        mergeServerAssetsIntoProject(p, rows);
        migrateProjectMediaLibraries(p);
        saveProjectsToStorage();
        if (state.activeProject && String(state.activeProject.id) === pid) {
            setTopbarInfo(p.name + " · " + projectMediaCount(p) + " 个素材");
            refreshMediaGridView();
            updateContextChips();
            renderProjects();
        } else {
            renderProjects();
        }
    } catch (e) {
        console.warn("[FrameOS] refreshServerAssetsForProject", pid, e);
        if (state.activeProject && String(state.activeProject.id) === pid) {
            showToast("同步服务端素材失败：" + (e && e.message ? e.message : String(e)));
        }
    }
}

function maxServerAssetIdOnProject(project) {
    const p = project || state.activeProject;
    if (!p || !Array.isArray(p.media)) return 0;
    let n = 0;
    for (const m of p.media) {
        const sid = Number(m._serverAssetId);
        if (Number.isFinite(sid) && sid > n) n = sid;
    }
    return n;
}

function syncPreviewCursorFromProject() {
    lastPreviewMaxServerAssetId = maxServerAssetIdOnProject();
}

/** 用于聊天区缩略条：带 url 的素材按服务端 id / 本地 id 排序 */
function sortedMediaForChatPreview(project) {
    if (!project || !Array.isArray(project.media) || project.media.length === 0) return [];
    const list = project.media.filter((m) => m && m.url);
    list.sort((a, b) => {
        const sa = Number(a._serverAssetId);
        const sb = Number(b._serverAssetId);
        if (Number.isFinite(sa) && Number.isFinite(sb) && sa !== sb) return sa - sb;
        if (Number.isFinite(sa) && !Number.isFinite(sb)) return -1;
        if (!Number.isFinite(sa) && Number.isFinite(sb)) return 1;
        return (Number(a.id) || 0) - (Number(b.id) || 0);
    });
    return list;
}

/** @param {Array<{ url?: string, type?: string }>} items */
function mediaItemsToChatPreviewParts(items) {
    const parts = [];
    for (const m of items) {
        const abs = resolveAssetPreviewUrl(m.url);
        if (!abs) continue;
        const typ = m.type || "image";
        if (typ === "video") {
            parts.push(
                `<video class="chat-gen-preview-vid" src="${escAttr(abs)}" muted playsinline controls preload="metadata" referrerpolicy="no-referrer"></video>`,
            );
        } else if (typ === "image" || typ === "storyboard") {
            if (isLikelyRasterImageUrl(abs)) {
                parts.push(
                    `<img class="chat-gen-preview-img" src="${escAttr(abs)}" alt="" loading="lazy" referrerpolicy="no-referrer">`,
                );
            }
        }
    }
    return parts;
}

function appendChatPreviewBubble(msgsEl, parts, metaKicker) {
    if (!msgsEl || !parts.length) return;
    const wrap = document.createElement("div");
    wrap.className = "msg agent msg--compact-media";
    const time = new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
    const kicker = escHtml(metaKicker);
    wrap.innerHTML = `<div class="msg-meta">${kicker} · ${time}</div><div class="msg-bubble msg-bubble--media-only"><div class="chat-gen-preview-row">${parts.join("")}</div></div>`;
    msgsEl.appendChild(wrap);
}

/** 本轮 Agent 成功后，在气泡下追加服务端新入库的图/视频缩略预览（不与历史消息一起持久化） */
function appendNewGenerationMediaPreviews() {
    if (!state.activeProject || !Array.isArray(state.activeProject.media)) return;
    const msgs = document.getElementById("chat-messages");
    if (!msgs) return;
    const t = lastPreviewMaxServerAssetId;
    const fresh = state.activeProject.media.filter((m) => {
        const sid = Number(m._serverAssetId);
        return m.source === "server" && Number.isFinite(sid) && sid > t;
    });
    if (!fresh.length) return;
    lastPreviewMaxServerAssetId = Math.max(
        t,
        ...fresh.map((m) => Number(m._serverAssetId) || 0),
    );
    fresh.sort((a, b) => (Number(a._serverAssetId) || 0) - (Number(b._serverAssetId) || 0));
    const parts = mediaItemsToChatPreviewParts(fresh);
    if (!parts.length) return;
    appendChatPreviewBubble(msgs, parts, "成片预览");
    msgs.scrollTop = msgs.scrollHeight;
    syncChatToolbarVisibility();
}

function loadProjectsFromStorage() {
    try {
        let saved = null;
        try {
            saved = sessionStorage.getItem(SS_ACTIVE_PROJECT) || localStorage.getItem(LS_ACTIVE_PROJECT);
        } catch (_) {}

        const raw = localStorage.getItem(LS_PROJECTS);
        if (!raw) {
            if (saved != null && saved !== "") ensureBackendLinkedProject(saved);
            return;
        }
        let parsed;
        try {
            parsed = JSON.parse(raw);
        } catch {
            console.warn("FrameOS: frameos_projects JSON 无效，保留当前列表");
            return;
        }
        if (!Array.isArray(parsed)) {
            if (saved != null && saved !== "") ensureBackendLinkedProject(saved);
            return;
        }
        /* 用户删光项目时会写入 []；必须覆盖内存里的内置三项目，否则刷新又变回 store 初始值 */
        if (parsed.length === 0) {
            state.projects = [];
            if (saved != null && saved !== "") ensureBackendLinkedProject(saved);
            return;
        }

        state.projects = parsed;
        state.projects.forEach(migrateProjectMediaLibraries);

        if (saved != null && saved !== "") {
            const hasSaved = state.projects.some((p) => String(p.id) === String(saved));
            if (!hasSaved) {
                console.warn(
                    "FrameOS: 项目列表里没有上次会话的 project_id=" +
                        saved +
                        "，已补充占位项目以拉取后端素材/聊天记录。",
                );
                ensureBackendLinkedProject(saved);
            }
        }
    } catch (e) {
        console.warn("FrameOS: restore projects failed", e);
    }
}

function saveProjectsToStorage() {
    try {
        localStorage.setItem(LS_PROJECTS, JSON.stringify(state.projects));
    } catch (e) {
        console.warn("FrameOS: save projects failed", e);
    }
}

/**
 * 当 localStorage 里的项目列表丢失、但 session 里仍记着后端的 project_id 时，
 * 补一条占位项目，避免 GET /assets 始终用错 id（例如列表为空却 session 里仍有旧 project_id）。
 */
function ensureBackendLinkedProject(rawId) {
    const sid = String(rawId).trim();
    if (!sid || state.projects.some((p) => String(p.id) === sid)) return;
    const id = /^\d+$/.test(sid) ? Number(sid) : sid;
    state.projects.unshift({
        id,
        name: "项目 · " + sid.slice(-8),
        color: "#6a6a6a",
        desc: "",
        media: [],
    });
    migrateProjectMediaLibraries(state.projects[0]);
    saveProjectsToStorage();
}

/** 从服务端拉取「有过对话」的 project_id，补进侧边栏（localStorage 空、换 127.0.0.1/localhost、清缓存后仍能加载历史） */
async function mergeServerChatProjectsIntoSidebar() {
    if (CONFIG.USE_MOCK_API) return;
    try {
        const ids = await fetchChatProjectIds();
        if (!Array.isArray(ids) || ids.length === 0) return;
        const have = new Set(state.projects.map((p) => String(p.id)));
        for (const raw of ids) {
            const sid = String(raw ?? "").trim();
            if (!sid || have.has(sid)) continue;
            ensureBackendLinkedProject(sid);
            have.add(sid);
        }
    } catch (e) {
        console.warn("FrameOS: mergeServerChatProjectsIntoSidebar", e);
    }
}

function rememberActiveProjectId(projectId) {
    const s = String(projectId);
    try {
        sessionStorage.setItem(SS_ACTIVE_PROJECT, s);
    } catch (_) {}
    try {
        localStorage.setItem(LS_ACTIVE_PROJECT, s);
    } catch (_) {}
}

function clearStoredActiveProjectId() {
    try {
        sessionStorage.removeItem(SS_ACTIVE_PROJECT);
    } catch (_) {}
    try {
        localStorage.removeItem(LS_ACTIVE_PROJECT);
    } catch (_) {}
}

function pickInitialProject() {
    const list = state.projects;
    if (!list || !list.length) return null;
    let saved = null;
    try {
        saved = sessionStorage.getItem(SS_ACTIVE_PROJECT) || localStorage.getItem(LS_ACTIVE_PROJECT);
    } catch (_) {}
    if (saved != null && saved !== "") {
        const match = list.find((p) => String(p.id) === String(saved));
        if (match) return match;
    }
    return list[0];
}

function agentWelcomeText(project) {
    const name = project && project.name ? project.name : "当前项目";
    return (
        "你好！我是 FrameOS Agent（项目「" +
        name +
        "」），可以帮你：\n" +
        "· 分析视频素材并生成分镜脚本\n" +
        "· 描述画面风格与运镜建议\n" +
        "· 生成 Sora / ComfyUI / 剪映 等平台提示词\n" +
        "· 管理项目素材与拼接任务\n\n" +
        "请告诉我你的创作需求。"
    );
}

function ensureAgentThread(projectId) {
    const k = String(projectId);
    if (!state.agentThreads[k]) state.agentThreads[k] = [];
    return state.agentThreads[k];
}

const CHAT_TOOLBAR_TOP_EPS = 3;

function syncChatScrollTopBtn() {
    const box = document.getElementById("chat-messages");
    const btn = document.getElementById("chat-scroll-top-btn");
    if (!box || !btn) return;
    const canScroll = box.scrollHeight > box.clientHeight + 8;
    const awayFromTop = box.scrollTop > 64;
    btn.classList.toggle("is-visible", canScroll && awayFromTop);
}

function syncChatToolbarVisibility() {
    const box = document.getElementById("chat-messages");
    if (!box) return;
    const toolbar = document.getElementById("chat-session-toolbar");
    if (toolbar) {
        const atTop = box.scrollTop <= CHAT_TOOLBAR_TOP_EPS;
        toolbar.classList.toggle("chat-toolbar--collapsed", !atTop);
    }
    syncChatScrollTopBtn();
}

function bindChatToolbarScrollReveal() {
    const box = document.getElementById("chat-messages");
    if (!box) return;
    box.addEventListener("scroll", syncChatToolbarVisibility, { passive: true });
    window.addEventListener("resize", syncChatToolbarVisibility, { passive: true });
}

function scrollChatToTop() {
    const box = document.getElementById("chat-messages");
    if (!box) return;
    box.scrollTo({ top: 0, behavior: "smooth" });
}

/** 后端 user/assistant → UI user/agent；按 seq、id 稳定排序，避免刷新后顺序漂移 */
function serverMessagesToThread(raw) {
    const arr = Array.isArray(raw) ? [...raw] : [];
    const hasSeq = arr.some(
        (m) => m && typeof m === "object" && m.seq != null && Number.isFinite(Number(m.seq)),
    );
    if (hasSeq) {
        arr.sort((a, b) => {
            const sa = Number(a.seq);
            const sb = Number(b.seq);
            if (sa !== sb) return sa - sb;
            return (Number(a.id) || 0) - (Number(b.id) || 0);
        });
    }
    const out = [];
    for (const m of arr) {
        if (!m || typeof m !== "object") continue;
        const role = m.role === "assistant" ? "agent" : "user";
        const text = m.content != null ? String(m.content) : "";
        out.push({ role, text });
    }
    return out;
}

/** 去掉首条欢迎语（仅 presets），便于与纯 DB 消息对齐比较 */
function stripLeadingWelcome(thread) {
    if (!thread || !thread.length) return [];
    const first = thread[0];
    if (first.role === "agent" && first.presets === true) return thread.slice(1);
    return thread.slice();
}

function normChatTextForMerge(t) {
    return String(t || "")
        .replace(/\r\n/g, "\n")
        .replace(/[ \t\f\v]+/g, " ")
        .trim();
}

function commonPrefixLen(serverThread, localNw) {
    let i = 0;
    const n = Math.min(serverThread.length, localNw.length);
    while (i < n) {
        const a = serverThread[i];
        const b = localNw[i];
        if (a.role !== b.role || normChatTextForMerge(a.text) !== normChatTextForMerge(b.text)) break;
        i += 1;
    }
    return i;
}

/**
 * 用 GET messages 结果更新内存线程。
 * 不能仅用条数比较：例如本地 [欢迎, 新用户话] 对应 DB 仍只有旧的多轮，server 更长时会误覆盖并丢掉未落库的用户句。
 */
function mergeFetchedAgentThread(pid, rawMessages) {
    /* 正在发 /api/agent/chat 时不要合并服务端快照，避免 resync/轮询尾包覆盖本地线程或反复 render 清掉「正在输入」 */
    if (state.agentTypingProjectId != null && String(state.agentTypingProjectId) === String(pid)) {
        return { keptLocal: true };
    }
    const serverThread =
        rawMessages && rawMessages.length > 0 ? serverMessagesToThread(rawMessages) : [];
    const fullLocal = state.agentThreads[pid] ? [...state.agentThreads[pid]] : [];
    const localNw = stripLeadingWelcome(fullLocal);
    const k = commonPrefixLen(serverThread, localNw);

    if (k === serverThread.length && localNw.length >= serverThread.length) {
        state.agentThreads[pid] = fullLocal;
        return { keptLocal: localNw.length > serverThread.length };
    }
    if (k === localNw.length && serverThread.length >= localNw.length) {
        state.agentThreads[pid] = serverThread;
        return { keptLocal: false };
    }
    if (localNw.length && localNw[localNw.length - 1].role === "user") {
        state.agentThreads[pid] = fullLocal;
        return { keptLocal: true };
    }
    /* 服务端条数更少且前缀对不齐时，不要用短列表覆盖长本地（避免切换项目再回来丢最近一轮） */
    if (localNw.length > serverThread.length) {
        state.agentThreads[pid] = fullLocal;
        return { keptLocal: true };
    }
    state.agentThreads[pid] = serverThread;
    return { keptLocal: false };
}

/** 服务端稍后才写入 DB 时，延迟再拉一次以对齐（仅一次，避免 Mock 空列表死循环） */
const agentThreadResyncTimers = {};

function scheduleAgentThreadResync(pid) {
    if (CONFIG.USE_MOCK_API) return;
    if (agentThreadResyncTimers[pid]) return;
    agentThreadResyncTimers[pid] = setTimeout(async () => {
        delete agentThreadResyncTimers[pid];
        try {
            if (state.agentTypingProjectId != null && String(state.agentTypingProjectId) === String(pid)) {
                return;
            }
            const { messages } = await fetchAgentMessages(pid);
            if (state.agentTypingProjectId != null && String(state.agentTypingProjectId) === String(pid)) {
                return;
            }
            mergeFetchedAgentThread(pid, messages);
            if (shouldStartAssistantPollForProject(pid)) {
                startPendingAssistantPoll(pid);
            } else {
                stopPendingAssistantPoll(pid);
            }
            if (state.activeProject && String(state.activeProject.id) === pid) {
                renderChatForProject(state.activeProject);
                syncPreviewCursorFromProject();
                updateStats();
            }
        } catch (_) {
            /* 忽略 */
        }
    }, 1600);
}

function clearAgentThreadResyncTimer(pid) {
    const t = agentThreadResyncTimers[pid];
    if (t) {
        clearTimeout(t);
        delete agentThreadResyncTimers[pid];
    }
}

/** 仅在不同项目之间切换时取消上一次拉取，避免同 id 连续加载（如重复 init）误 Abort 导致不渲染 */
let selectProjectLoadAbort = null;
let selectProjectLoadPid = null;
/** 与 ``_serverAssetId`` 对齐：仅对本轮新入库的素材在聊天里插预览，避免重复 */
let lastPreviewMaxServerAssetId = 0;

/** 服务端已写入 user、助手尚未落库时（如刷新页面），轮询 messages 直到出现 assistant */
const pendingAssistantPollByPid = {};

function stopPendingAssistantPoll(pid) {
    const spid = String(pid);
    const m = pendingAssistantPollByPid[spid];
    if (m && m.iv) clearInterval(m.iv);
    delete pendingAssistantPollByPid[spid];
}

function stopAllPendingAssistantPolls() {
    for (const k of Object.keys(pendingAssistantPollByPid)) {
        stopPendingAssistantPoll(k);
    }
}

/** 合并后的线程末尾是否为「已发用户句、尚无助手回复」 */
function threadEndsWithPendingUser(pid) {
    const nw = stripLeadingWelcome(state.agentThreads[pid] || []);
    const last = nw[nw.length - 1];
    return !!(last && last.role === "user");
}

function shouldStartAssistantPollForProject(pid) {
    if (CONFIG.USE_MOCK_API) return false;
    if (state.agentTypingProjectId && String(state.agentTypingProjectId) === String(pid)) return false;
    return threadEndsWithPendingUser(pid);
}

function startPendingAssistantPoll(pid) {
    if (CONFIG.USE_MOCK_API) return;
    if (state.agentTypingProjectId && String(state.agentTypingProjectId) === String(pid)) return;
    const spid = String(pid);
    stopPendingAssistantPoll(spid);
    let attempts = 0;
    const maxAttempts = 45;
    const tick = async () => {
        attempts += 1;
        if (attempts > maxAttempts) {
            stopPendingAssistantPoll(spid);
            if (state.activeProject && String(state.activeProject.id) === spid) {
                renderChatForProject(state.activeProject);
                showToast("长时间未同步到助手回复，请检查后端或重新发送上一条消息。");
            }
            return;
        }
        try {
            const { messages } = await fetchAgentMessages(spid);
            if (!pendingAssistantPollByPid[spid]) return;
            if (state.agentTypingProjectId != null && String(state.agentTypingProjectId) === String(spid)) {
                return;
            }
            mergeFetchedAgentThread(spid, messages);
            const nw = stripLeadingWelcome(state.agentThreads[spid] || []);
            const last = nw[nw.length - 1];
            const gotAssistant = last && last.role === "assistant";
            if (state.activeProject && String(state.activeProject.id) === spid) {
                renderChatForProject(state.activeProject);
                syncPreviewCursorFromProject();
                updateStats();
            }
            if (gotAssistant) {
                stopPendingAssistantPoll(spid);
                const proj = state.projects.find((x) => String(x.id) === spid);
                if (proj) await refreshServerAssetsForProject(proj);
            }
        } catch (_) {
            /* 单次轮询失败忽略 */
        }
    };
    pendingAssistantPollByPid[spid] = {
        iv: setInterval(() => {
            void tick();
        }, 2000),
    };
    void tick();
}

async function selectProject(p) {
    migrateProjectMediaLibraries(p);
    rememberActiveProjectId(p.id);
    state.libraryBulkSelectedIds = {};
    state.activeProject = p;
    renderProjects();
    setTopbarInfo(p.name + " · " + projectMediaCount(p) + " 个素材");
    const title = document.getElementById("project-panel-title");
    if (title) title.textContent = p.name;
    currentMediaLibraryFilter = "all";
    document.querySelectorAll(".filter-tab").forEach((t, i) => t.classList.toggle("active", i === 0));
    refreshMediaGridView();
    updateContextChips();

    const pid = String(p.id);
    stopAllPendingAssistantPolls();
    if (selectProjectLoadAbort && selectProjectLoadPid !== pid) {
        selectProjectLoadAbort.abort();
    }
    selectProjectLoadPid = pid;
    const loadCtrl = new AbortController();
    selectProjectLoadAbort = loadCtrl;

    function isCurrentProject() {
        return state.activeProject && String(state.activeProject.id) === pid;
    }

    try {
        const { messages } = await fetchAgentMessages(pid, loadCtrl.signal);
        if (!isCurrentProject()) return;
        const { keptLocal } = mergeFetchedAgentThread(pid, messages);
        if (keptLocal) {
            scheduleAgentThreadResync(pid);
        } else {
            clearAgentThreadResyncTimer(pid);
        }

        const assetsRaw = await fetchProjectAssets(pid, loadCtrl.signal);
        if (!isCurrentProject()) return;
        if (Array.isArray(assetsRaw) && assetsRaw.length > 0 && state.activeProject) {
            mergeServerAssetsIntoProject(state.activeProject, assetsRaw);
            migrateProjectMediaLibraries(state.activeProject);
            saveProjectsToStorage();
            setTopbarInfo(state.activeProject.name + " · " + projectMediaCount(state.activeProject) + " 个素材");
            refreshMediaGridView();
            updateContextChips();
            renderProjects();
        }
    } catch (e) {
        if (e && e.name === "AbortError") {
            /* 仍要渲染：Abort 时若用户仍停留在该项目，避免聊天区空白 */
        } else {
            console.error(e);
            if (!isCurrentProject()) {
                /* skip toast */
            } else {
                showToast("加载对话记录失败：" + (e && e.message ? e.message : String(e)));
                if (!state.agentThreads[pid]) state.agentThreads[pid] = [];
            }
        }
    } finally {
        if (selectProjectLoadAbort === loadCtrl) {
            selectProjectLoadAbort = null;
            selectProjectLoadPid = null;
        }
    }

    if (!isCurrentProject()) return;
    renderChatForProject(p);
    syncPreviewCursorFromProject();
    if (shouldStartAssistantPollForProject(pid)) {
        startPendingAssistantPoll(pid);
    }
}

/** 切换项目时重绘聊天区：每个项目独立一条消息列表 */
function renderChatForProject(project) {
    const box = document.getElementById("chat-messages");
    if (!box) return;
    if (!project) {
        box.innerHTML =
            '<div class="chat-empty-hint" style="padding:1.5rem 1rem;text-align:center;color:var(--muted);font-size:0.6875rem;letter-spacing:0.06em;">请从左侧选择项目，或点击「新建项目」开始对话</div>';
        updateStats();
        requestAnimationFrame(() => {
            syncChatToolbarVisibility();
        });
        return;
    }
    const pid = String(project.id);
    const thread = ensureAgentThread(pid);
    if (thread.length === 0) {
        thread.push({ role: "agent", text: agentWelcomeText(project), presets: true });
    }
    box.innerHTML = "";
    const last = thread[thread.length - 1];
    /* 仅本页正在请求 /api/agent/chat 时显示「正在输入」。轮询拉消息不算进行中请求，避免 DB 仅末条 user 时假死在这一态。 */
    const pendingUserWhileTyping =
        state.agentTypingProjectId === pid && last && last.role === "user";
    const threadBeforePreview = pendingUserWhileTyping ? thread.slice(0, -1) : thread;
    for (const m of threadBeforePreview) {
        appendMessage(m.role, m.text, {
            noPersist: true,
            initialPresets: m.presets === true,
        });
    }
    const previewParts = mediaItemsToChatPreviewParts(sortedMediaForChatPreview(project));
    if (previewParts.length) {
        appendChatPreviewBubble(box, previewParts, "素材预览");
    }
    if (pendingUserWhileTyping) {
        appendMessage(last.role, last.text, { noPersist: true });
        appendMessageRaw(`<div class="msg agent" id="${CHAT_TYPING_DOM_ID}">
    <div class="msg-meta">FrameOS Agent · 正在输入</div>
    <div class="msg-bubble agent-typing"><span class="cursor"></span></div>
  </div>`);
    }
    updateStats();
    requestAnimationFrame(() => {
        box.scrollTop = box.scrollHeight;
        syncChatToolbarVisibility();
    });
}

// ===== INIT =====
async function init() {
    state.agentTypingProjectId = null;
    stopAllPendingAssistantPolls();
    loadProjectsFromStorage();
    state.projects.forEach(migrateProjectMediaLibraries);
    try {
        if (!localStorage.getItem(LS_PROJECTS)) {
            saveProjectsToStorage();
        }
    } catch (_) {}
    /* 先按本地列表选项目并拉 messages，再合并服务端 project_id。避免 chat-project-ids 挂起/慢时整页阻塞、activeProject 一直为空导致聊天请求从不发出 */
    renderProjects();
    startSessionTimer();
    let initial = pickInitialProject();
    if (initial) await selectProject(initial);

    await mergeServerChatProjectsIntoSidebar();
    renderProjects();
    if (!state.activeProject) {
        initial = pickInitialProject();
        if (initial) await selectProject(initial);
    }
}

function renderProjects() {
    const list = document.getElementById('projects-list');
    if (!list) return;
    list.innerHTML = '';
    state.projects.forEach(p => {
        const div = document.createElement('div');
        div.className = 'project-item' + (state.activeProject && state.activeProject.id === p.id ? ' active' : '');
        const color = document.createElement('span');
        color.className = 'project-color';
        color.style.background = p.color;
        const name = document.createElement('span');
        name.className = 'project-name';
        name.textContent = p.name;
        const count = document.createElement('span');
        count.className = 'project-count';
        count.textContent = String(projectMediaCount(p));
        const delBtn = document.createElement('button');
        delBtn.type = 'button';
        delBtn.className = 'project-delete-btn';
        delBtn.setAttribute('aria-label', '删除项目');
        delBtn.title = '删除项目（含本地列表与服务器对话、素材记录）';
        delBtn.textContent = '×';
        delBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            void deleteProjectAndData(p);
        });
        div.appendChild(color);
        div.appendChild(name);
        div.appendChild(count);
        div.appendChild(delBtn);
        div.onclick = () => selectProject(p);
        list.appendChild(div);
    });
}

// ===== TABS =====
function switchTab(tab) {
    state.activeTab = tab;
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.getElementById('panel-' + tab).classList.add('active');
    document.getElementById('tab-' + tab).classList.add('active');
    // Update nav items
    const navLabels = {
        agent: 'Agent 对话',
        project: '项目素材',
        reverse: '逆向提示词',
        douyin: '抖音视频',
        splice: '视频拼接',
    };
    document.querySelectorAll('.nav-item').forEach(n => {
        if (n.textContent.trim().startsWith(navLabels[tab].slice(0, 3))) n.classList.add('active');
    });
    if (tab === "project" && state.activeProject) {
        void refreshServerAssetsForActiveProject();
    }
    if (tab === "douyin" && state.activeProject) {
        void refreshServerAssetsForActiveProject();
    }
}

// ===== MEDIA GRID =====
const TYPE_ICONS = { video: '🎬', image: '🖼', audio: '🎵', storyboard: '📑' };
const TYPE_COLORS = { video: '#3a3a3a', image: '#003a5a', audio: '#3a2a00', storyboard: '#5a3000' };

/** 前端在 8080、接口在 8000 时，把 /media/... 拼到 API 根上 */
function resolveAssetPreviewUrl(raw) {
    if (raw == null || raw === "") return "";
    const u = String(raw).trim();
    if (!u) return "";
    if (/^https?:\/\//i.test(u) || u.startsWith("//") || u.startsWith("data:") || u.startsWith("blob:")) return u;
    if (u.startsWith("/")) {
        const base = CONFIG.API_BASE_URL.replace(/\/$/, "");
        return base + u;
    }
    return u;
}

function isLikelyRasterImageUrl(u) {
    const path = String(u).split("?")[0].toLowerCase();
    if (path.endsWith(".json") || path.endsWith(".txt")) return false;
    if (/\.(png|jpe?g|gif|webp|bmp|svg)$/.test(path)) return true;
    if (/^https?:\/\//i.test(u)) return true;
    return u.startsWith("/");
}

function mediaItemCanLargeView(m) {
    if (!m || !m.url) return false;
    const t = m.type || "image";
    if (t !== "image" && t !== "storyboard") return false;
    const abs = resolveAssetPreviewUrl(m.url);
    return !!(abs && isLikelyRasterImageUrl(abs));
}

function mediaCardZoomBtnHtml(m) {
    if (!mediaItemCanLargeView(m)) return "";
    const id = Number(m.id);
    if (!Number.isFinite(id)) return "";
    const glyph = `<svg class="media-zoom-btn__glyph" width="14" height="14" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><path d="M8 4H4v4M16 4h4v4M4 16v4h4M20 16v4h-4" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round"/><circle cx="12" cy="12" r="2.25" stroke="currentColor" stroke-width="1.65"/></svg>`;
    return `<button type="button" class="media-zoom-btn" aria-label="浏览大图" title="浏览大图" onclick="event.stopPropagation();openMediaLightbox(${id})">${glyph}</button>`;
}

/** 视频卡片角标：一键加入拼接时间轴（不打开详情） */
function mediaQuickTimelineBtnHtml(m) {
    if (!isTimelineVideoClip(m)) return "";
    const id = Number(m.id);
    if (!Number.isFinite(id)) return "";
    return `<button type="button" class="media-quick-timeline" title="加入拼接时间轴" aria-label="加入拼接时间轴" onclick="event.stopPropagation();quickAddVideoToTimeline(${id})">＋轨</button>`;
}

function updateLibraryBulkToolbar() {
    const el = document.getElementById("library-bulk-count");
    if (!el) return;
    const n = Object.keys(state.libraryBulkSelectedIds).length;
    el.textContent = n ? `已选 ${n}` : "";
}

/** 视频：右上角方框＝批量入轨勾选；非视频：同方框表示当前详情选中（点卡片选中） */
function toggleVideoBulkSelectFromEl(el) {
    if (!(el instanceof HTMLElement)) return;
    const sid = el.dataset.bulkId;
    if (!sid) return;
    if (state.libraryBulkSelectedIds[sid]) delete state.libraryBulkSelectedIds[sid];
    else state.libraryBulkSelectedIds[sid] = true;
    updateLibraryBulkToolbar();
    refreshMediaGridView();
}

function mediaCheckSlotHtml(m) {
    const isSelected = state.selectedMedia && state.selectedMedia.id === m.id;
    if (isTimelineVideoClip(m) && m.id !== undefined && m.id !== null) {
        const sid = String(m.id);
        const bulk = !!state.libraryBulkSelectedIds[sid];
        return `<div class="media-check media-check--toggle-bulk${bulk ? " is-on" : ""}" role="checkbox" aria-checked="${bulk}" title="勾选后加入「选中入轨并拼接」" data-bulk-id="${escAttr(sid)}" onclick="event.stopPropagation();toggleVideoBulkSelectFromEl(this)">${bulk ? "✓" : ""}</div>`;
    }
    return `<div class="media-check">${isSelected ? "✓" : ""}</div>`;
}

function selectAllVisibleVideosInLibrary() {
    if (!state.activeProject) {
        showToast("请先选择项目");
        return;
    }
    const view = getActiveProjectMediaView();
    let n = 0;
    for (const m of view) {
        if (!isTimelineVideoClip(m)) continue;
        if (!resolveClipPlaybackUrl(m)) continue;
        state.libraryBulkSelectedIds[String(m.id)] = true;
        n++;
    }
    if (!n) {
        showToast("当前列表没有可入轨的视频");
        return;
    }
    refreshMediaGridView();
    showToast(`已勾选 ${n} 个视频`);
}

function clearLibraryBulkSelection() {
    state.libraryBulkSelectedIds = {};
    refreshMediaGridView();
}

/** 将勾选的视频按当前列表顺序加入时间轴，并打开拼接页 */
function addBulkSelectedVideosToTimelineAndGoSplice() {
    if (!state.activeProject) {
        showToast("请先选择项目");
        return;
    }
    const keys = Object.keys(state.libraryBulkSelectedIds);
    if (!keys.length) {
        showToast("请先勾选视频");
        return;
    }
    const keySet = new Set(keys);
    const view = getActiveProjectMediaView();
    let added = 0;
    let skippedOnTimeline = 0;
    let skippedNoUrl = 0;
    for (const m of view) {
        if (!keySet.has(String(m.id))) continue;
        if (!isTimelineVideoClip(m)) continue;
        if (!resolveClipPlaybackUrl(m)) {
            skippedNoUrl++;
            continue;
        }
        if (state.timeline.find((t) => t.id === m.id)) {
            skippedOnTimeline++;
            continue;
        }
        state.timeline.push({ ...m, width: 120 + Math.random() * 80 | 0 });
        added++;
    }
    if (!added) {
        showToast(
            skippedNoUrl
                ? "所勾选项无播放地址或不可预览，请等待同步"
                : skippedOnTimeline
                  ? "所选视频已在时间轴中"
                  : "没有可添加的视频",
        );
        return;
    }
    state.timelineSelectedIndex = state.timeline.length - 1;
    state.selectedMedia = state.timeline[state.timelineSelectedIndex];
    renderTimeline();
    state.libraryBulkSelectedIds = {};
    refreshMediaGridView();
    switchTab("splice");
    showToast(`已添加 ${added} 个视频到时间轴`);
}

let _mediaLightboxEsc = null;

function openMediaLightbox(id) {
    const m = state.activeProject?.media?.find((x) => x.id === id);
    if (!mediaItemCanLargeView(m)) {
        showToast("当前素材无法预览大图");
        return;
    }
    const overlay = document.getElementById("media-lightbox-overlay");
    const img = document.getElementById("media-lightbox-img");
    const cap = document.getElementById("media-lightbox-caption");
    if (!overlay || !img || !cap) return;
    img.src = resolveAssetPreviewUrl(m.url);
    img.alt = m.name || "";
    cap.textContent = m.name || "";
    overlay.style.display = "flex";
    overlay.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
    if (_mediaLightboxEsc) {
        document.removeEventListener("keydown", _mediaLightboxEsc);
    }
    _mediaLightboxEsc = (e) => {
        if (e.key === "Escape") closeMediaLightbox();
    };
    document.addEventListener("keydown", _mediaLightboxEsc);
}

function openMediaLightboxFromDetail() {
    if (!state.selectedMedia) return;
    openMediaLightbox(state.selectedMedia.id);
}

function closeMediaLightbox() {
    const overlay = document.getElementById("media-lightbox-overlay");
    if (!overlay) return;
    overlay.style.display = "none";
    overlay.setAttribute("aria-hidden", "true");
    document.body.style.overflow = "";
    const img = document.getElementById("media-lightbox-img");
    if (img) {
        img.removeAttribute("src");
        img.alt = "";
    }
    const cap = document.getElementById("media-lightbox-caption");
    if (cap) cap.textContent = "";
    if (_mediaLightboxEsc) {
        document.removeEventListener("keydown", _mediaLightboxEsc);
        _mediaLightboxEsc = null;
    }
}

function mediaThumbInnerHtml(m) {
    const t = m.type || "image";
    const col = TYPE_COLORS[t] || "#333";
    const icon = TYPE_ICONS[t] || "📁";
    const abs = resolveAssetPreviewUrl(m.url);
    if (t === "video" && abs) {
        return `<video class="media-thumb-video" src="${escAttr(abs)}" muted playsinline preload="metadata" referrerpolicy="no-referrer"></video>`;
    }
    if ((t === "image" || t === "storyboard") && abs && isLikelyRasterImageUrl(abs)) {
        return `<img class="media-thumb-img" src="${escAttr(abs)}" alt="" loading="lazy" referrerpolicy="no-referrer">`;
    }
    return `<div class="media-thumb-inner" style="background:${col}22;">
  <span style="font-size:1.5rem">${icon}</span>
  <span>${String(t).toUpperCase()}</span>
</div>`;
}

function renderDetailPreview(el, m) {
    const t = m.type || "image";
    el.style.background = TYPE_COLORS[t] || "#111";
    const abs = resolveAssetPreviewUrl(m.url);
    if (t === "video" && abs) {
        el.innerHTML = `<video class="detail-preview-media" src="${escAttr(abs)}" controls playsinline referrerpolicy="no-referrer"></video>`;
        return;
    }
    if ((t === "image" || t === "storyboard") && abs && isLikelyRasterImageUrl(abs)) {
        el.innerHTML = `<img class="detail-preview-media" src="${escAttr(abs)}" alt="" referrerpolicy="no-referrer">`;
        return;
    }
    el.innerHTML = "";
    el.textContent =
        t === "video"
            ? "▶ VIDEO PREVIEW"
            : t === "audio"
              ? "♪ AUDIO"
              : t === "storyboard"
                ? "◧ STORYBOARD / JSON"
                : "◼ IMAGE PREVIEW";
}

/** Agent 气泡：普通文本转义；将 ![alt](url) 渲染为内联预览（与左侧素材区互补） */
function agentBubbleHtml(raw) {
    const t = String(raw ?? "");
    if (!t.trim()) return "";
    const pattern = /!\[([^\]]*)\]\((https?:[^)\s]+)\)/gi;
    const matches = [];
    let mm;
    pattern.lastIndex = 0;
    while ((mm = pattern.exec(t)) !== null) {
        matches.push(mm);
    }
    if (matches.length === 0) {
        return escHtml(t);
    }
    let out = "";
    let last = 0;
    for (const m of matches) {
        const idx = m.index ?? 0;
        if (idx > last) {
            out += escHtml(t.slice(last, idx));
        }
        const u = resolveAssetPreviewUrl(m[2]);
        out += `<div class="chat-inline-media"><img src="${escAttr(u)}" alt="${escAttr(m[1] || "")}" loading="lazy" referrerpolicy="no-referrer"></div>`;
        last = idx + m[0].length;
    }
    if (last < t.length) {
        out += escHtml(t.slice(last));
    }
    return out;
}

function getActiveProjectMediaView() {
    if (!state.activeProject) return [];
    migrateProjectMediaLibraries(state.activeProject);
    const all = state.activeProject.media;
    if (currentMediaLibraryFilter === "all") return all;
    return all.filter((m) => inferMediaLibrary(m) === currentMediaLibraryFilter);
}

function refreshMediaGridView() {
    const grid = document.getElementById("media-grid");
    if (!state.activeProject) {
        state.libraryBulkSelectedIds = {};
        updateLibraryBulkToolbar();
        if (grid) {
            grid.innerHTML =
                '<div style="grid-column:1/-1;padding:2rem;text-align:center;color:var(--muted);font-size:0.6875rem;letter-spacing:0.06em;">← 从左侧选择项目，或新建项目</div>';
        }
        return;
    }
    renderMediaGrid(getActiveProjectMediaView());
    updateLibraryBulkToolbar();
}

function renderMediaGrid(media) {
    const grid = document.getElementById('media-grid');
    if (!media || !media.length) {
        grid.innerHTML = '<div style="grid-column:1/-1;padding:2rem;text-align:center;color:var(--muted);font-size:0.6875rem;">此项目暂无素材，点击「导入素材」添加</div>';
        return;
    }
    let html = `<div class="drop-zone" onclick="document.getElementById('file-upload').click()">
    <div class="drop-zone-icon">↑</div>
    拖拽文件至此处，或点击导入素材
  </div>`;
    media.forEach(m => {
        const isSelected = state.selectedMedia && state.selectedMedia.id === m.id;
        const t = m.type || 'image';
        html += `<div class="media-card ${isSelected ? 'selected' : ''}" onclick="selectMedia(${m.id})" ondblclick="openMediaDetail(${m.id})">
      <div class="media-thumb">
${mediaThumbInnerHtml(m)}
${mediaCardZoomBtnHtml(m)}
${mediaQuickTimelineBtnHtml(m)}
<span class="thumb-type-badge ${t}">${t}</span>
${m.dur ? `<span class="thumb-duration">${m.dur}</span>` : ''}
      </div>
${mediaCheckSlotHtml(m)}
      <div class="media-info">
<div class="media-name" title="${m.name}">${m.name}</div>
<div class="media-meta">${m.size || '—'} ${m.res || m.dur || ''}</div>
      </div>
    </div>`;
    });
    grid.innerHTML = html;
}

function selectMedia(id) {
    const m = state.activeProject.media.find(x => x.id === id);
    state.selectedMedia = m;
    refreshMediaGridView();
}

function openMediaDetail(id) {
    const m = state.activeProject.media.find(x => x.id === id);
    if (!m) return;
    state.selectedMedia = m;
    refreshMediaGridView();
    const detail = document.getElementById('media-detail');
    detail.style.display = 'flex';
    document.getElementById('detail-filename').textContent = m.name;
    const t = m.type || 'image';
    const prevEl = document.getElementById("detail-preview");
    renderDetailPreview(prevEl, m);
    const lbBtn = document.getElementById("detail-btn-lightbox");
    if (mediaItemCanLargeView(m)) {
        prevEl.classList.add("detail-preview--zoomable");
        prevEl.title = "点击浏览大图";
        prevEl.onclick = () => openMediaLightbox(m.id);
        if (lbBtn) lbBtn.style.display = "";
    } else {
        prevEl.classList.remove("detail-preview--zoomable");
        prevEl.removeAttribute("title");
        prevEl.onclick = null;
        if (lbBtn) lbBtn.style.display = "none";
    }
    const meta = document.getElementById('detail-meta');
    let rows = [
        ['所属库', MEDIA_LIB_LABEL[inferMediaLibrary(m)] || inferMediaLibrary(m)],
        ['类型', String(t).toUpperCase()],
        ['文件名', m.name],
        ['大小', m.size || '—'],
    ];
    if (m.dur) rows.push(['时长', m.dur]);
    if (m.res) rows.push(['分辨率', m.res]);
    meta.innerHTML = rows.map(([k, v]) => `<div class="detail-row"><span class="detail-key">${k}</span><span class="detail-val">${v}</span></div>`).join('');
}

function closeMediaDetail() {
    document.getElementById('media-detail').style.display = 'none';
    const prevEl = document.getElementById("detail-preview");
    if (prevEl) {
        prevEl.classList.remove("detail-preview--zoomable");
        prevEl.removeAttribute("title");
        prevEl.onclick = null;
    }
}

function filterMedia(libraryOrAll, btn) {
    document.querySelectorAll('.filter-tab').forEach(t => t.classList.remove('active'));
    btn.classList.add('active');
    if (!state.activeProject) return;
    currentMediaLibraryFilter = libraryOrAll;
    refreshMediaGridView();
}

function sortMedia() {
    if (!state.activeProject) return;
    state.activeProject.media.sort((a, b) => a.name.localeCompare(b.name));
    refreshMediaGridView();
    saveProjectsToStorage();
}

function handleFileUpload(e) {
    if (!state.activeProject) { alert('请先选择项目'); return; }
    const files = Array.from(e.target.files);
    files.forEach((f, i) => {
        const type = f.type.startsWith('video') ? 'video' : f.type.startsWith('image') ? 'image' : 'audio';
        const id = Date.now() + i;
        const library = type === 'video' ? 'video' : 'asset';
        const objectUrl = URL.createObjectURL(f);
        state.activeProject.media.push({
            id,
            name: f.name,
            type,
            size: formatSize(f.size),
            library,
            url: objectUrl,
            _localObjectUrl: true,
        });
    });
    refreshMediaGridView();
    renderProjects();
    setTopbarInfo(state.activeProject.name + ' · ' + projectMediaCount(state.activeProject) + ' 个素材');
    updateContextChips();
    saveProjectsToStorage();
}

function addToTimeline() {
    if (!state.selectedMedia) return;
    const m = state.selectedMedia;
    if (state.timeline.find(t => t.id === m.id)) { return; }
    state.timeline.push({ ...m, width: 120 + Math.random() * 80 | 0 });
    state.timelineSelectedIndex = state.timeline.length - 1;
    renderTimeline();
    showToast("已加入时间轴");
}

/** 素材库卡片「＋轨」：不经过详情直接入轨 */
function quickAddVideoToTimeline(mediaId) {
    if (!state.activeProject) {
        showToast("请先选择项目");
        return;
    }
    const m = state.activeProject.media.find((x) => x.id === mediaId);
    if (!m) return;
    if (!isTimelineVideoClip(m)) {
        showToast("仅视频可加入时间轴");
        return;
    }
    if (!resolveClipPlaybackUrl(m)) {
        showToast("视频地址未就绪，请等待同步后重试");
        return;
    }
    if (state.timeline.find((t) => t.id === m.id)) {
        showToast("该视频已在时间轴中");
        return;
    }
    state.selectedMedia = m;
    state.timeline.push({ ...m, width: 120 + Math.random() * 80 | 0 });
    state.timelineSelectedIndex = state.timeline.length - 1;
    renderTimeline();
    showToast("已加入时间轴");
    refreshMediaGridView();
}

/** 工具栏：当前筛选列表里尚未入轨的视频全部加入 */
function addAllVisibleVideosToTimeline() {
    if (!state.activeProject) {
        showToast("请先选择项目");
        return;
    }
    const candidates = getActiveProjectMediaView().filter(
        (m) => isTimelineVideoClip(m) && resolveClipPlaybackUrl(m),
    );
    if (!candidates.length) {
        showToast("当前列表没有可入轨的视频");
        return;
    }
    let added = 0;
    for (const m of candidates) {
        if (state.timeline.find((t) => t.id === m.id)) continue;
        state.timeline.push({ ...m, width: 120 + Math.random() * 80 | 0 });
        added++;
    }
    if (!added) {
        showToast("列表中的视频已全部在时间轴");
        return;
    }
    state.timelineSelectedIndex = state.timeline.length - 1;
    renderTimeline();
    showToast(`已加入 ${added} 个视频到时间轴`);
    refreshMediaGridView();
}

function deleteSelected() {
    if (!state.selectedMedia || !state.activeProject) return;
    const victim = state.selectedMedia;
    if (victim._localObjectUrl && victim.url) {
        try {
            URL.revokeObjectURL(victim.url);
        } catch (_) { /* ignore */ }
    }
    state.activeProject.media = state.activeProject.media.filter(m => m.id !== victim.id);
    state.timeline = state.timeline.filter((t) => t.id !== victim.id);
    if (state.timelineSelectedIndex >= state.timeline.length) {
        state.timelineSelectedIndex = Math.max(0, state.timeline.length - 1);
    }
    renderTimeline();
    state.selectedMedia = null;
    closeMediaDetail();
    refreshMediaGridView();
    renderProjects();
    setTopbarInfo(state.activeProject.name + ' · ' + projectMediaCount(state.activeProject) + ' 个素材');
    updateContextChips();
    saveProjectsToStorage();
}

function analyzeSelected() {
    if (!state.selectedMedia) return;
    switchTab('reverse');
    document.getElementById('rev-desc').value = `分析素材：${state.selectedMedia.name}\n类型：${state.selectedMedia.type}\n${state.selectedMedia.res ? '分辨率：' + state.selectedMedia.res : ''}`;
}

// ===== TIMELINE =====
function isTimelineVideoClip(c) {
    return !!(c && (c.type === "video" || c.library === "video"));
}

/** 从 currentIdx 之后找第一个可播放的视频轨道索引，没有则 -1 */
function findNextPlayableVideoIndex(currentIdx) {
    let idx = currentIdx + 1;
    while (idx < state.timeline.length) {
        const c = state.timeline[idx];
        if (isTimelineVideoClip(c) && resolveClipPlaybackUrl(c)) return idx;
        idx++;
    }
    return -1;
}

function getSpliceVideoEls() {
    return {
        a: document.getElementById("splice-preview-video"),
        b: document.getElementById("splice-preview-video-b"),
    };
}

/** 当前可见、带控件的一层（另一层预载下一段） */
function getSpliceForegroundEl() {
    const { a, b } = getSpliceVideoEls();
    if (!a) return b;
    if (!b) return a;
    return a.classList.contains("is-splice-buffer-back") ? b : a;
}

function getSpliceBackgroundEl() {
    const { a, b } = getSpliceVideoEls();
    if (!a || !b) return null;
    return a.classList.contains("is-splice-buffer-back") ? a : b;
}

function syncSpliceVideoControls() {
    const { a, b } = getSpliceVideoEls();
    if (!a || !b) return;
    const fg = getSpliceForegroundEl();
    a.toggleAttribute("controls", a === fg);
    b.toggleAttribute("controls", b === fg);
}

/** 在后台 video 上预解码下一段，片尾切换层时无需对前台换 src */
function prefetchNextSpliceSegment() {
    const bg = getSpliceBackgroundEl();
    if (!bg || !state.timeline.length) return;
    const nextIdx = findNextPlayableVideoIndex(state.timelineSelectedIndex);
    if (nextIdx < 0) {
        bg.removeAttribute("src");
        delete bg.dataset.timelineIndex;
        try {
            bg.load();
        } catch (_) { /* ignore */ }
        return;
    }
    const u = resolveClipPlaybackUrl(state.timeline[nextIdx]);
    if (!u) return;
    if (bg.dataset.timelineIndex === String(nextIdx)) return;
    bg.dataset.timelineIndex = String(nextIdx);
    bg.src = u;
    try {
        bg.load();
    } catch (_) { /* ignore */ }
}

/** 与素材库同步后的最新 url（时间轴里存的是加入时的快照） */
function resolveClipPlaybackUrl(clip) {
    if (!clip) return "";
    const proj = state.activeProject;
    if (proj && Array.isArray(proj.media)) {
        const fresh = proj.media.find((m) => m.id === clip.id);
        const raw = (fresh && fresh.url) || clip.url || "";
        return raw ? resolveAssetPreviewUrl(String(raw)) : "";
    }
    const raw = clip.url || "";
    return raw ? resolveAssetPreviewUrl(String(raw)) : "";
}

function formatSpliceTime(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return "00:00";
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
}

function updateSplicePreviewChrome() {
    const v = getSpliceForegroundEl();
    const t = document.getElementById("preview-time");
    if (!t) return;
    if (!state.timeline.length) {
        t.textContent = "00:00 / 00:00";
        return;
    }
    if (v && Number.isFinite(v.duration) && v.duration > 0) {
        t.textContent = formatSpliceTime(v.currentTime) + " / " + formatSpliceTime(v.duration);
        return;
    }
    let totalSecs = 0;
    for (const c of state.timeline) totalSecs += parseDur(c.dur) || 10;
    t.textContent = "00:00 / " + formatDuration(totalSecs);
}

function setSplicePlayButtonState(playing) {
    const b = document.getElementById("play-btn");
    if (!b) return;
    const label = b.querySelector(".sp-transport-play-label");
    const icon = b.querySelector(".sp-transport-play-icon");
    if (playing) {
        b.classList.add("is-playing");
        if (label) label.textContent = "暂停";
        if (icon) icon.textContent = "⏸";
    } else {
        b.classList.remove("is-playing");
        if (label) label.textContent = "播放";
        if (icon) icon.textContent = "▶";
    }
}

function updateSpliceTransportLabels() {
    const badge = document.getElementById("sp-clip-badge");
    const nw = document.getElementById("sp-now-playing");
    const n = state.timeline.length;
    const i = state.timelineSelectedIndex;
    if (badge) badge.textContent = n ? `${i + 1} / ${n}` : "— / —";
    if (nw) {
        nw.textContent =
            n && state.timeline[i] ? String(state.timeline[i].name || "未命名") : "尚未选择片段";
        if (n && state.timeline[i]) nw.title = String(state.timeline[i].name || "");
        else nw.removeAttribute("title");
    }
}

function onSplicePreviewEnded(ev) {
    const endedEl = ev.target;
    if (!(endedEl instanceof HTMLVideoElement)) return;
    if (endedEl !== getSpliceForegroundEl()) return;
    const nextIdx = findNextPlayableVideoIndex(state.timelineSelectedIndex);
    if (nextIdx < 0) {
        setSplicePlayButtonState(false);
        updateSplicePreviewChrome();
        return;
    }
    const fg = getSpliceForegroundEl();
    const bg = getSpliceBackgroundEl();
    if (!fg || !bg) return;
    const nextSrc = resolveClipPlaybackUrl(state.timeline[nextIdx]);
    if (!nextSrc) {
        setSplicePlayButtonState(false);
        return;
    }
    const proceed = () => {
        state.timelineSelectedIndex = nextIdx;
        document.querySelectorAll(".tl-clip").forEach((c) => c.classList.remove("selected"));
        const clipEl = document.getElementById("clip-" + nextIdx);
        if (clipEl) clipEl.classList.add("selected");
        try {
            fg.pause();
        } catch (_) { /* ignore */ }
        bg.currentTime = 0;
        fg.classList.add("is-splice-buffer-back");
        bg.classList.remove("is-splice-buffer-back");
        syncSpliceVideoControls();
        updateSpliceTransportLabels();
        updateSplicePreviewChrome();
        prefetchNextSpliceSegment();
        const p = bg.play();
        if (p !== undefined) p.catch(() => setSplicePlayButtonState(false));
    };
    const indexMatch = bg.dataset.timelineIndex === String(nextIdx);
    if (indexMatch && (bg.src || bg.currentSrc)) {
        if (bg.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) {
            proceed();
        } else {
            bg.addEventListener("canplay", proceed, { once: true });
        }
        return;
    }
    bg.dataset.timelineIndex = String(nextIdx);
    bg.src = nextSrc;
    bg.addEventListener("canplay", proceed, { once: true });
    try {
        bg.load();
    } catch (_) { /* ignore */ }
}

function bindSplicePreviewVideoOnce() {
    const { a, b } = getSpliceVideoEls();
    if (!a || !b || a.dataset.spliceBound) return;
    a.dataset.spliceBound = "1";
    b.dataset.spliceBound = "1";
    for (const v of [a, b]) {
        v.addEventListener("timeupdate", (e) => {
            if (e.target !== getSpliceForegroundEl()) return;
            updateSplicePreviewChrome();
        });
        v.addEventListener("loadedmetadata", (e) => {
            if (e.target !== getSpliceForegroundEl()) return;
            updateSplicePreviewChrome();
        });
        v.addEventListener("play", (e) => {
            if (e.target === getSpliceForegroundEl()) setSplicePlayButtonState(true);
        });
        v.addEventListener("pause", (e) => {
            if (e.target === getSpliceForegroundEl()) setSplicePlayButtonState(false);
        });
        v.addEventListener("ended", onSplicePreviewEnded);
    }
    syncSpliceVideoControls();
}

/**
 * 为当前索引加载 <video> 源；silent 时不弹 Toast（用于切换片段自动换源）。
 */
function ensureSpliceVideoForIndex(idx, opts = {}) {
    const silent = !!opts.silent;
    const { a, b } = getSpliceVideoEls();
    const fg = getSpliceForegroundEl();
    const ph = document.getElementById("preview-placeholder");
    if (!fg || !state.timeline.length || idx < 0 || idx >= state.timeline.length) return false;
    const clip = state.timeline[idx];
    if (!isTimelineVideoClip(clip)) {
        if (!silent) showToast("该片段不是视频，无法预览");
        for (const v of [a, b]) {
            if (!v) continue;
            try {
                v.pause();
            } catch (_) { /* ignore */ }
            v.removeAttribute("src");
            delete v.dataset.timelineIndex;
            v.style.display = "none";
        }
        if (ph) ph.style.display = "flex";
        return false;
    }
    const src = resolveClipPlaybackUrl(clip);
    if (!src) {
        if (!silent) showToast("该视频暂无播放地址，请等待素材同步后重试");
        for (const v of [a, b]) {
            if (!v) continue;
            try {
                v.pause();
            } catch (_) { /* ignore */ }
            v.removeAttribute("src");
            delete v.dataset.timelineIndex;
            v.style.display = "none";
        }
        if (ph) ph.style.display = "flex";
        return false;
    }
    if (a && b) {
        a.style.display = "block";
        b.style.display = "block";
    }
    if (fg.dataset.timelineIndex !== String(idx)) {
        fg.src = src;
        fg.dataset.timelineIndex = String(idx);
    }
    if (ph) ph.style.display = "none";
    prefetchNextSpliceSegment();
    syncSpliceVideoControls();
    return true;
}

function renderTimeline() {
    bindSplicePreviewVideoOnce();
    const row = document.getElementById('tl-video-row');
    const tray = document.getElementById('tray-items');
    const totalEl = document.getElementById('tray-total-dur');
    const ph = document.getElementById('preview-placeholder');
    const pv = document.getElementById('splice-preview-video');
    const pvb = document.getElementById('splice-preview-video-b');

    if (!state.timeline.length) {
        row.innerHTML = `<div class="tl-add-zone" onclick="switchTab('project')">+ 从素材库添加</div>`;
        tray.innerHTML = '';
        totalEl.textContent = '0s';
        if (ph) ph.style.display = 'flex';
        document.getElementById('preview-controls').style.display = 'none';
        if (pv) {
            try {
                pv.pause();
            } catch (_) { /* ignore */ }
            pv.removeAttribute('src');
            delete pv.dataset.timelineIndex;
            pv.style.display = 'none';
            pv.classList.remove('is-splice-buffer-back');
        }
        if (pvb) {
            try {
                pvb.pause();
            } catch (_) { /* ignore */ }
            pvb.removeAttribute('src');
            delete pvb.dataset.timelineIndex;
            pvb.style.display = 'none';
            pvb.classList.add('is-splice-buffer-back');
        }
        syncSpliceVideoControls();
        state.timelineSelectedIndex = 0;
        setSplicePlayButtonState(false);
        updateSpliceTransportLabels();
        updateSplicePreviewChrome();
        return;
    }

    if (state.timelineSelectedIndex >= state.timeline.length) {
        state.timelineSelectedIndex = state.timeline.length - 1;
    }
    if (state.timelineSelectedIndex < 0) state.timelineSelectedIndex = 0;

    let totalSecs = 0;
    row.innerHTML = state.timeline.map((c, i) => {
        const secs = parseDur(c.dur) || 10;
        totalSecs += secs;
        const w = Math.max(80, secs * 8 * state.tlZoom);
        return `<div class="tl-clip" style="width:${w}px;" onclick="selectClip(${i})" id="clip-${i}">
      <div class="tl-clip-name">${c.name}</div>
      <div class="tl-clip-dur">${c.dur || '—'}</div>
      <div class="tl-clip-bar" style="width:${w * 0.7}px;"></div>
    </div>`;
    }).join('') + `<div class="tl-add-zone" onclick="switchTab('project')">+</div>`;

    tray.innerHTML = state.timeline.map((c, i) =>
        `<div class="tray-item">
      <span class="tray-item-num">${i + 1}</span>
      <span class="tray-item-name" title="${c.name}">${c.name}</span>
      <span class="tray-item-dur">${c.dur || '—'}</span>
      <button class="tray-remove" onclick="removeClip(${i})">✕</button>
    </div>`
    ).join('');

    totalEl.textContent = formatDuration(totalSecs);
    document.getElementById('preview-controls').style.display = 'block';
    ensureSpliceVideoForIndex(state.timelineSelectedIndex, { silent: true });
    updateSplicePreviewChrome();
    updateSpliceTransportLabels();
    document.querySelectorAll('.tl-clip').forEach(c => c.classList.remove('selected'));
    const sel = document.getElementById('clip-' + state.timelineSelectedIndex);
    if (sel) sel.classList.add('selected');
}

function selectClip(i) {
    if (i < 0 || i >= state.timeline.length) return;
    state.timelineSelectedIndex = i;
    document.querySelectorAll('.tl-clip').forEach(c => c.classList.remove('selected'));
    const el = document.getElementById('clip-' + i);
    if (el) el.classList.add('selected');
    ensureSpliceVideoForIndex(i, { silent: true });
    updateSplicePreviewChrome();
    updateSpliceTransportLabels();
    prefetchNextSpliceSegment();
}

function removeClip(i) {
    state.timeline.splice(i, 1);
    if (state.timelineSelectedIndex >= state.timeline.length) {
        state.timelineSelectedIndex = Math.max(0, state.timeline.length - 1);
    } else if (i < state.timelineSelectedIndex) {
        state.timelineSelectedIndex--;
    }
    renderTimeline();
}

function clearTimeline() {
    state.timeline = [];
    renderTimeline();
}

function autoArrange() {
    // Shuffle as demo
    state.timeline.sort((a, b) => a.name.localeCompare(b.name));
    renderTimeline();
}

function addTransition() {
    const t = document.getElementById('transition-select').value;
    showToast('已在片段间添加转场：' + t);
}

function addTextClip() {
    const text = prompt('输入字幕文字：');
    if (!text) return;
    const row = document.getElementById('tl-text-row');
    const clip = document.createElement('div');
    clip.className = 'tl-clip';
    clip.style.width = '140px';
    clip.style.borderColor = 'var(--accent2)';
    clip.innerHTML = `<div class="tl-clip-name">${text}</div><div class="tl-clip-dur">字幕</div><div class="tl-clip-bar" style="width:98px;background:var(--accent2);"></div>`;
    const addZone = row.querySelector('.tl-add-zone');
    row.insertBefore(clip, addZone || null);
}

function zoomTimeline(dir) {
    state.tlZoom = Math.max(0.5, Math.min(3, state.tlZoom + dir * 0.25));
    renderTimeline();
}

function previewToggle() {
    bindSplicePreviewVideoOnce();
    if (!state.timeline.length) {
        showToast('请先添加片段');
        return;
    }
    const idx = state.timelineSelectedIndex;
    const v = getSpliceForegroundEl();
    if (!ensureSpliceVideoForIndex(idx)) return;
    if (v.paused) {
        v.play().catch((e) => showToast('播放失败：' + (e && e.message ? e.message : String(e))));
    } else {
        v.pause();
    }
}

function seekPrev() {
    if (!state.timeline.length) return;
    let i = state.timelineSelectedIndex - 1;
    if (i < 0) i = state.timeline.length - 1;
    const v = getSpliceForegroundEl();
    const wasPlaying = v && !v.paused;
    selectClip(i);
    if (wasPlaying) {
        const fg = getSpliceForegroundEl();
        if (fg) fg.play().catch(() => {});
    }
}

function seekNext() {
    if (!state.timeline.length) return;
    let i = state.timelineSelectedIndex + 1;
    if (i >= state.timeline.length) i = 0;
    const v = getSpliceForegroundEl();
    const wasPlaying = v && !v.paused;
    selectClip(i);
    if (wasPlaying) {
        const fg = getSpliceForegroundEl();
        if (fg) fg.play().catch(() => {});
    }
}

async function exportVideo() {
    if (!state.timeline.length) { showToast('请先添加视频片段'); return; }
    const res = document.getElementById('res-select').value;
    try {
        const out = await requestExportVideo({
            resolution: res,
            clipIds: state.timeline.map((c) => String(c.id)),
        });
        showToast(out.jobId ? "任务已创建: " + out.jobId : `正在准备导出 ${res} 视频`);
    } catch (e) {
        showToast("导出请求失败: " + (e && e.message ? e.message : String(e)));
    }
}

// ===== AGENT CHAT =====

function sendPreset(text) {
    document.getElementById('chat-input').value = text;
    sendChatMessage();
}

function handleChatKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendChatMessage();
    }
}

async function sendChatMessage() {
    if (!state.activeProject) {
        showToast("请先选择一个项目");
        return;
    }
    const input = document.getElementById('chat-input');
    const text = input.value.trim();
    if (!text) return;
    input.value = '';

    const project = state.activeProject;
    const projectId = String(project.id);
    const projectName = project.name;

    stopPendingAssistantPoll(projectId);
    clearAgentThreadResyncTimer(projectId);
    appendMessageForProject(projectId, "user", text);

    state.agentTypingProjectId = projectId;
    if (state.activeProject && String(state.activeProject.id) === projectId) {
        document.getElementById(CHAT_TYPING_DOM_ID)?.remove();
        appendMessageRaw(`<div class="msg agent" id="${CHAT_TYPING_DOM_ID}">
    <div class="msg-meta">FrameOS Agent · 正在输入</div>
    <div class="msg-bubble agent-typing"><span class="cursor"></span></div>
  </div>`);
    }

    try {
        const resp = await requestAgentReply({
            text,
            projectName,
            projectId,
        });
        appendMessageForProject(projectId, "agent", resp);
        updateStats();
        const projRef = state.projects.find((x) => String(x.id) === projectId);
        if (projRef) {
            await refreshServerAssetsForProject(projRef);
        }
        if (state.activeProject && String(state.activeProject.id) === projectId) {
            appendNewGenerationMediaPreviews();
        }
    } catch (err) {
        const isAbort = err && err.name === "AbortError";
        appendMessageForProject(
            projectId,
            "agent",
            isAbort
                ? "请求超时（约 " + String(Math.round(CONFIG.REQUEST_TIMEOUT_MS / 1000)) + " 秒无响应），请重试。"
                : "请求失败：" + (err && err.message ? err.message : String(err)),
        );
        updateStats();
    } finally {
        state.agentTypingProjectId = null;
        document.getElementById(CHAT_TYPING_DOM_ID)?.remove();
        stopPendingAssistantPoll(projectId);
    }
}

/**
 * @param {string|number} projectId
 * @param {'user'|'agent'} role
 * @param {string} text
 * @param {{ noPersist?: boolean, initialPresets?: boolean }} [opts]
 */
function appendMessageForProject(projectId, role, text, opts) {
    if (projectId == null || projectId === "") return;
    const pid = String(projectId);
    if (!opts?.noPersist) {
        const row = { role, text };
        if (opts?.initialPresets) row.presets = true;
        ensureAgentThread(pid).push(row);
    }
    if (!state.activeProject || String(state.activeProject.id) !== pid) return;
    const msgs = document.getElementById("chat-messages");
    if (!msgs) return;
    const div = document.createElement("div");
    div.className = "msg " + role;
    const time = new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
    const presetRow =
        role === "agent" && opts?.initialPresets
            ? `<button class="msg-action-btn" onclick="sendPreset('分析当前项目的素材风格')">分析素材风格</button>
      <button class="msg-action-btn" onclick="sendPreset('帮我生成一段30秒的短片脚本')">生成脚本</button>
      <button class="msg-action-btn" onclick="sendPreset('推荐适合婚礼纪录片的运镜方式')">运镜建议</button>
      `
            : "";
    div.innerHTML = `
    <div class="msg-meta">${role === "user" ? "你" : "FrameOS Agent"} · ${time}</div>
    <div class="msg-bubble">${role === "agent" ? agentBubbleHtml(text) : escHtml(text)}</div>
    ${role === "agent" ? `<div class="msg-actions">
      ${presetRow}
      <button class="msg-action-btn" onclick="sendPreset('继续详细说明')">继续说明</button>
      <button class="msg-action-btn" onclick="copyText(this)">复制</button>
    </div>` : ""}
  `;
    msgs.appendChild(div);
    msgs.scrollTop = msgs.scrollHeight;
    syncChatToolbarVisibility();
}

/**
 * @param {'user'|'agent'} role
 * @param {string} text
 * @param {{ noPersist?: boolean, initialPresets?: boolean }} [opts] noPersist：仅渲染 DOM，不写 state（用于切换项目时重放）
 */
function appendMessage(role, text, opts) {
    if (!state.activeProject) return;
    appendMessageForProject(state.activeProject.id, role, text, opts);
}

function appendMessageRaw(html) {
    const msgs = document.getElementById('chat-messages');
    msgs.insertAdjacentHTML('beforeend', html);
    msgs.scrollTop = msgs.scrollHeight;
    syncChatToolbarVisibility();
}

async function clearChat() {
    if (!state.activeProject) {
        showToast("请先选择一个项目");
        return;
    }
    const pid = String(state.activeProject.id);
    let serverOk = true;
    try {
        await clearAgentSession(pid);
    } catch (e) {
        serverOk = false;
        showToast("服务端会话未清除：" + (e && e.message ? e.message : String(e)));
    }
    state.agentThreads[pid] = [];
    state.agentTypingProjectId = null;
    stopPendingAssistantPoll(pid);
    document.getElementById(CHAT_TYPING_DOM_ID)?.remove();
    renderChatForProject(state.activeProject);
    if (serverOk) showToast("已清空本会话");
}

function openLocalDataPanel() {
    document.getElementById("local-data-overlay")?.classList.add("open");
}

function closeLocalDataPanel() {
    document.getElementById("local-data-overlay")?.classList.remove("open");
}

async function clearChatFromPanel() {
    await clearChat();
    closeLocalDataPanel();
}

function confirmResetBrowserWorkspace() {
    if (
        !confirm(
            "确定重置本机工作台？将清除浏览器内保存的项目列表与偏好，不删除服务器上的聊天记录。"
        )
    ) {
        return;
    }
    void resetBrowserWorkspace();
}

async function resetBrowserWorkspace() {
    try {
        localStorage.removeItem(LS_PROJECTS);
        localStorage.removeItem(LS_ACTIVE_PROJECT);
        sessionStorage.removeItem(SS_ACTIVE_PROJECT);
    } catch (_) {}
    stopAllPendingAssistantPolls();
    state.agentThreads = {};
    state.agentTypingProjectId = null;
    state.projects = [];
    state.activeProject = null;
    state.selectedMedia = null;
    closeMediaDetail();
    closeLocalDataPanel();
    renderProjects();
    setTopbarInfo("未选择项目");
    const title = document.getElementById("project-panel-title");
    if (title) title.textContent = "选择项目";
    renderChatForProject(null);
    refreshMediaGridView();
    updateContextChips();
    updateStats();
    showToast("本机工作台已重置");
}

function exportChat() { showToast('对话已复制到剪贴板（演示）'); }
function copyText(btn) { showToast('已复制'); }

function updateStats() {
    const n = state.activeProject
        ? (state.agentThreads[String(state.activeProject.id)]?.length ?? 0)
        : 0;
    state.chatMsgs = n;
    document.getElementById('stat-msgs').textContent = String(n);
    document.getElementById('stat-tokens').textContent = '~' + (n * 180 + (Math.random() * 50 | 0));
}

// ===== DOUYIN =====

async function submitDouyinFetch() {
    if (!state.activeProject) {
        showToast("请先选择一个项目");
        return;
    }
    const ta = document.getElementById("dy-share-text");
    const share = (ta?.value ?? "").trim();
    if (!share) {
        ta?.focus();
        showToast("请粘贴分享链接或口令");
        return;
    }
    const title = (document.getElementById("dy-video-title")?.value ?? "").trim();
    const btn = document.getElementById("dy-fetch-btn");
    const statusEl = document.getElementById("dy-status");
    const v = document.getElementById("dy-preview-video");
    const empty = document.getElementById("dy-preview-empty");
    const stage = document.getElementById("dy-preview-stage");
    const shell = document.getElementById("dy-video-shell");
    if (btn) {
        btn.disabled = true;
        btn.textContent = "下载中…";
    }
    if (statusEl) statusEl.textContent = "正在解析并下载…";
    try {
        const r = await requestDouyinFetch({
            project_id: String(state.activeProject.id),
            share_text: share,
            video_title: title,
        });
        const url = resolveAssetPreviewUrl(r.uri);
        if (v && url) {
            v.src = url;
            stage?.classList.add("has-video");
            if (shell) shell.hidden = false;
            if (empty) empty.hidden = true;
        }
        if (statusEl) {
            statusEl.textContent = "已保存到视频库 · " + new Date().toLocaleTimeString();
        }
        showToast("已写入视频库");
        await refreshServerAssetsForActiveProject();
    } catch (err) {
        const msg = err && err.message ? err.message : String(err);
        if (statusEl) statusEl.textContent = "失败：" + msg;
        showToast("下载失败：" + msg);
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = "下载到视频库";
        }
    }
}

// ===== REVERSE =====

async function analyzeVideo() {
    const desc = document.getElementById('rev-desc').value.trim();
    if (!desc) { document.getElementById('rev-desc').focus(); return; }

    const btn = document.getElementById('analyze-btn');
    btn.disabled = true;
    btn.textContent = '分析中...';
    document.getElementById('rev-status').textContent = '正在生成...';
    document.getElementById('json-status').className = 'json-status processing';
    document.getElementById('json-status').textContent = '处理中';

    try {
        const style = document.getElementById('rev-style').value;
        const platform = document.getElementById('rev-platform').value;
        const scenes = parseInt(document.getElementById('rev-scenes').value, 10);
        const data = await requestStoryboardJson({
            description: desc,
            style,
            platform,
            sceneCount: scenes,
        });
        state.jsonData = data;
        renderJsonVisual(data);
        renderJsonRaw(data);
        document.getElementById('rev-status').textContent = '已生成 · ' + new Date().toLocaleTimeString();
        document.getElementById('json-status').className = 'json-status ready';
        document.getElementById('json-status').textContent = '已完成';
    } catch (err) {
        document.getElementById('rev-status').textContent = '失败：' + (err && err.message ? err.message : String(err));
        document.getElementById('json-status').className = 'json-status';
        document.getElementById('json-status').textContent = '错误';
    } finally {
        btn.disabled = false;
        btn.textContent = '生成分镜 JSON →';
    }
}

function renderJsonVisual(data) {
    const container = document.getElementById('visual-content');
    const metaHtml = `<div style="background:var(--surface);border:var(--hairline) solid var(--border);border-radius:var(--radius);padding:0.75rem;margin-bottom:0.75rem;">
    <div class="section-label" style="margin-bottom:0.4rem;">元数据</div>
    ${Object.entries(data.metadata).map(([k, v]) => `<div class="detail-row"><span class="detail-key">${k}</span><span class="detail-val">${v}</span></div>`).join('')}
  </div>`;
    const typeMap = { establishing: 'scene', detail: 'scene', human: 'dialogue', abstract: 'action', closing: 'scene' };
    const scenesHtml = data.scenes.map(s => `
    <div class="scene-card">
      <div class="scene-card-header">
<span class="scene-num">${s.scene_id}</span>
<span class="scene-timecode">${s.timecode}</span>
<span class="scene-type-badge ${typeMap[s.type] || 'scene'}">${s.type}</span>
      </div>
      <div class="scene-card-body">
<div class="scene-field-row">
  <div class="scene-field-key">画面描述</div>
  <div class="scene-field-val">${s.description}</div>
</div>
<div class="scene-field-row">
  <div class="scene-field-key">AI 提示词</div>
  <div class="prompt-text">${s.prompt}</div>
</div>
${s.dialogue ? `<div class="scene-field-row">
  <div class="scene-field-key">台词</div>
  <div class="scene-field-val" style="color:var(--accent2)">${s.dialogue}</div>
</div>` : ''}
<div class="scene-field-row">
  <div class="scene-field-key">运镜</div>
  <div class="scene-field-val">${s.camera.movement} · ${s.camera.angle} · ${s.camera.lens}</div>
</div>
<div class="scene-field-row">
  <div class="scene-field-key">情绪</div>
  <div class="scene-field-val" style="color:var(--muted)">${s.mood}</div>
</div>
      </div>
    </div>
  `).join('');
    container.innerHTML = metaHtml + scenesHtml;
}

function renderJsonRaw(data) {
    const raw = document.getElementById('raw-content');
    raw.innerHTML = syntaxHighlight(JSON.stringify(data, null, 2));
}

function syntaxHighlight(json) {
    return json
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g, m => {
            let cls = 'json-num';
            if (/^"/.test(m)) { cls = /:$/.test(m) ? 'json-key' : 'json-str'; }
            else if (/true|false/.test(m)) { cls = 'json-bool'; }
            else if (/null/.test(m)) { cls = 'json-null'; }
            return `<span class="${cls}">${m}</span>`;
        });
}

function switchJsonView(view, btn) {
    state.jsonView = view;
    document.querySelectorAll('.json-view-tab').forEach(t => t.classList.remove('active'));
    btn.classList.add('active');
    document.querySelectorAll('.json-content').forEach(c => c.classList.remove('active'));
    document.getElementById('json-' + view).classList.add('active');
}

function copyJson() {
    if (!state.jsonData) { showToast('暂无 JSON 数据'); return; }
    const text = JSON.stringify(state.jsonData, null, 2);
    navigator.clipboard.writeText(text).then(() => showToast('JSON 已复制到剪贴板')).catch(() => showToast('复制失败，请手动复制'));
}

// ===== PROJECT MODAL =====
function openNewProjectModal() {
    document.getElementById('modal-overlay').classList.add('open');
    setTimeout(() => document.getElementById('new-proj-name').focus(), 50);
}
function closeModal() { document.getElementById('modal-overlay').classList.remove('open'); }
function selectColor(el) {
    document.querySelectorAll('.color-swatch').forEach(s => s.classList.remove('active'));
    el.classList.add('active');
    state.selectedColor = el.dataset.color;
}
function createProject() {
    const name = document.getElementById('new-proj-name').value.trim();
    if (!name) { document.getElementById('new-proj-name').focus(); return; }
    const desc = document.getElementById('new-proj-desc').value.trim();
    const proj = { id: Date.now(), name, color: state.selectedColor, desc, media: [] };
    state.projects.push(proj);
    saveProjectsToStorage();
    renderProjects();
    selectProject(proj);
    closeModal();
    document.getElementById('new-proj-name').value = '';
    document.getElementById('new-proj-desc').value = '';
    switchTab('project');
}

/**
 * 删除项目：请求服务端清除该 project_id 的对话与素材表行，并从前端列表、localStorage、内存线程中移除。
 */
async function deleteProjectAndData(project) {
    if (!project) return;
    const name = project.name || "该项目";
    const pid = String(project.id);
    if (
        !confirm(
            `确定删除「${name}」？\n\n将同时删除：本机项目与素材列表、该项目的 Agent 对话记录（服务器数据库）。此操作不可恢复。`,
        )
    ) {
        return;
    }
    if (selectProjectLoadAbort && selectProjectLoadPid === pid) {
        try {
            selectProjectLoadAbort.abort();
        } catch (_) {}
        selectProjectLoadAbort = null;
        selectProjectLoadPid = null;
    }
    try {
        await deleteProjectRemote(pid);
    } catch (e) {
        console.warn("[FrameOS] deleteProjectRemote", pid, e);
        showToast("服务端删除失败：" + (e && e.message ? e.message : String(e)) + "（仍将移除本机列表）");
    }
    clearAgentThreadResyncTimer(pid);
    stopPendingAssistantPoll(pid);
    if (String(state.agentTypingProjectId) === pid) {
        state.agentTypingProjectId = null;
        document.getElementById(CHAT_TYPING_DOM_ID)?.remove();
    }
    delete state.agentThreads[pid];
    state.projects = state.projects.filter((p) => String(p.id) !== pid);
    saveProjectsToStorage();

    const wasActive = state.activeProject && String(state.activeProject.id) === pid;
    if (wasActive) {
        state.activeProject = null;
        state.selectedMedia = null;
        clearStoredActiveProjectId();
        closeMediaDetail();
        const next = state.projects[0] || null;
        if (next) {
            await selectProject(next);
        } else {
            setTopbarInfo("未选择项目");
            const title = document.getElementById("project-panel-title");
            if (title) title.textContent = "选择项目";
            renderProjects();
            renderChatForProject(null);
            refreshMediaGridView();
            updateContextChips();
            updateStats();
        }
    } else {
        renderProjects();
    }
    showToast("已删除项目「" + name + "」");
}

// ===== CONTEXT CHIPS =====
function updateContextChips() {
    const el = document.getElementById('context-chips');
    if (!el) return;
    if (!state.activeProject) {
        el.innerHTML =
            '<div class="context-chip" style="color:var(--muted);font-size:0.6rem;">暂无项目上下文 · 请选择或新建项目</div>';
        return;
    }
    migrateProjectMediaLibraries(state.activeProject);
    const c = { video: 0, asset: 0, storyboard: 0 };
    for (const m of state.activeProject.media) {
        const k = inferMediaLibrary(m);
        if (c[k] != null) c[k] += 1;
    }
    el.innerHTML = `<div class="context-chip">
    <span class="chip-color" style="background:${state.activeProject.color}"></span>
    <span style="font-size:0.6875rem">${state.activeProject.name}</span>
    <span class="chip-x">✕</span>
  </div>
  <div class="context-chip" style="font-size:0.6rem;color:var(--muted);">
    ${projectMediaCount(state.activeProject)} 条 · ${c.video} 视频库 · ${c.asset} 素材库 · ${c.storyboard} 分镜库
  </div>`;
}

/**
 * 后端生成物回调：写入指定项目下的视频库 / 素材库 / 分镜库（与 project_id 绑定）。
 * @param {{ projectId?: string|number, library: 'video'|'asset'|'storyboard', type?: string, name: string, size?: string, dur?: string, res?: string, url?: string, source?: string, id?: number }} payload
 */
function addGeneratedAssetToActiveProject(payload) {
    const lib = payload.library;
    if (lib !== "video" && lib !== "asset" && lib !== "storyboard") {
        showToast("library 须为 video | asset | storyboard");
        return null;
    }
    const pid =
        payload.projectId != null && payload.projectId !== ""
            ? String(payload.projectId)
            : state.activeProject
              ? String(state.activeProject.id)
              : "";
    if (!pid) {
        showToast("未指定 projectId 且当前无选中项目");
        return null;
    }
    const project = state.projects.find((p) => String(p.id) === pid);
    if (!project) {
        showToast("找不到项目 " + pid);
        return null;
    }
    const id = payload.id != null ? payload.id : Date.now() + (Math.random() * 1000 | 0);
    const t =
        payload.type ||
        (lib === "video" ? "video" : lib === "storyboard" ? "storyboard" : "image");
    const item = {
        id,
        name: payload.name || "未命名",
        type: t,
        library: lib,
        size: payload.size || "—",
        dur: payload.dur,
        res: payload.res,
        url: payload.url || "",
        source: payload.source || "agent",
    };
    project.media.push(item);
    migrateProjectMediaLibraries(project);
    saveProjectsToStorage();
    renderProjects();
    if (state.activeProject && String(state.activeProject.id) === String(project.id)) {
        setTopbarInfo(project.name + " · " + projectMediaCount(project) + " 个素材");
        updateContextChips();
        refreshMediaGridView();
    }
    showToast(`已入库「${MEDIA_LIB_LABEL[lib]}」：${item.name}`);
    return item;
}

// ===== SESSION TIMER =====
function startSessionTimer() {
    setInterval(() => {
        const s = Math.floor((Date.now() - state.sessionStart) / 1000);
        const m = Math.floor(s / 60), sec = s % 60;
        document.getElementById('stat-time').textContent = String(m).padStart(2, '0') + ':' + String(sec).padStart(2, '0');
    }, 1000);
}

// ===== TOAST =====
function showToast(msg) {
    const t = document.createElement('div');
    t.style.cssText = `position:fixed;bottom:1.5rem;right:1.5rem;background:var(--fg);color:var(--bg);font-family:var(--font-ui);font-size:0.8125rem;font-weight:500;letter-spacing:0.02em;padding:0.55rem 1rem;border-radius:0.4375rem;z-index:999;opacity:0;transition:opacity 0.2s;pointer-events:none;box-shadow:0 8px 24px color-mix(in oklab,var(--fg) 22%,transparent);`;
    t.textContent = msg;
    document.body.appendChild(t);
    requestAnimationFrame(() => { t.style.opacity = '1'; });
    setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 200); }, 2200);
}

/** 供 index.html 内联 onclick 使用（后续可改为 data-action + 事件委托） */
export function mountFrameOS() {
    document.getElementById("modal-overlay")?.addEventListener("click", function (e) {
        if (e.target === this) closeModal();
    });
    document.getElementById("local-data-overlay")?.addEventListener("click", function (e) {
        if (e.target === this) closeLocalDataPanel();
    });
    document.querySelectorAll(".output-opt").forEach((opt) => {
        opt.addEventListener("click", () => opt.classList.toggle("active"));
    });

    bindChatToolbarScrollReveal();
    syncChatToolbarVisibility();
    bindSplicePreviewVideoOnce();

    Object.assign(window, {
        switchTab,
        selectMedia,
        openMediaDetail,
        closeMediaDetail,
        openMediaLightbox,
        openMediaLightboxFromDetail,
        closeMediaLightbox,
        filterMedia,
        sortMedia,
        handleFileUpload,
        addToTimeline,
        quickAddVideoToTimeline,
        addAllVisibleVideosToTimeline,
        toggleVideoBulkSelectFromEl,
        selectAllVisibleVideosInLibrary,
        clearLibraryBulkSelection,
        addBulkSelectedVideosToTimelineAndGoSplice,
        analyzeSelected,
        deleteSelected,
        previewToggle,
        seekPrev,
        seekNext,
        clearTimeline,
        zoomTimeline,
        autoArrange,
        addTransition,
        addTextClip,
        exportVideo,
        sendPreset,
        sendChatMessage,
        handleChatKey,
        clearChat,
        clearChatFromPanel,
        exportChat,
        copyText,
        analyzeVideo,
        submitDouyinFetch,
        switchJsonView,
        copyJson,
        openNewProjectModal,
        closeModal,
        openLocalDataPanel,
        closeLocalDataPanel,
        confirmResetBrowserWorkspace,
        selectColor,
        createProject,
        removeClip,
        selectClip,
        addGeneratedAssetToActiveProject,
        scrollChatToTop,
        deleteProjectAndData,
    });

    void init().catch((e) => console.warn("FrameOS: init failed", e));
}
