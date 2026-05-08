/** 前端单一数据源；接后端后可在此做 hydrate / 同步 */
export const state = {
    activeTab: "agent",
    activeProject: null,
    projects: [],
    selectedMedia: null,
    /** 素材库批量入轨：{ [mediaId 字符串]: true }，不入本地存储 */
    libraryBulkSelectedIds: {},
    timeline: [],
    /** 时间轴上当前选中的片段索引（用于预览播放 / 上一段下一段） */
    timelineSelectedIndex: 0,
    selectedColor: "#3a6e00",
    /** 按项目 id 隔离的 Agent 对话：{ [projectId: string]: Array<{ role: 'user'|'agent', text: string }> } */
    agentThreads: {},
    /** 哪一项目正等待 Agent 回复（用于切回项目时还原「正在输入」） */
    agentTypingProjectId: null,
    chatMsgs: 0,
    sessionStart: Date.now(),
    jsonData: null,
    tlZoom: 1,
    jsonView: "visual",
};
