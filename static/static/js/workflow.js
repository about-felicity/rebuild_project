        // ===== STATE =====
        const state = {
            activeTab: 'agent',
            activeProject: null,
            projects: [
                {
                    id: 1, name: '城市夜景', color: '#3a6e00', desc: '延时摄影素材', media: [
                        { id: 1, name: 'night_timelapse_01.mp4', type: 'video', size: '1.2GB', dur: '0:45', res: '4K' },
                        { id: 2, name: 'traffic_flow.mp4', type: 'video', size: '890MB', dur: '1:20', res: '1080p' },
                        { id: 3, name: 'skyline_sunset.jpg', type: 'image', size: '12MB', res: '6000×4000' },
                        { id: 4, name: 'neon_reflections.jpg', type: 'image', size: '8MB', res: '4000×3000' },
                        { id: 5, name: 'ambient_city.wav', type: 'audio', size: '45MB', dur: '3:20' },
                    ]
                },
                {
                    id: 2, name: '婚礼纪录', color: '#7a4800', desc: '婚礼现场素材', media: [
                        { id: 6, name: 'ceremony_wide.mp4', type: 'video', size: '2.1GB', dur: '2:10', res: '4K' },
                        { id: 7, name: 'couple_portrait.jpg', type: 'image', size: '15MB', res: '5000×3333' },
                        { id: 8, name: 'wedding_march.mp3', type: 'audio', size: '8MB', dur: '3:45' },
                    ]
                },
                {
                    id: 3, name: '产品广告', color: '#005a8a', desc: '品牌商业宣传', media: [
                        { id: 9, name: 'product_hero.mp4', type: 'video', size: '450MB', dur: '0:30', res: '4K' },
                        { id: 10, name: 'lifestyle_01.jpg', type: 'image', size: '9MB', res: '4500×3000' },
                    ]
                },
            ],
            selectedMedia: null,
            timeline: [],
            selectedColor: '#3a6e00',
            chatMsgs: 2,
            sessionStart: Date.now(),
            jsonData: null,
            tlZoom: 1,
            jsonView: 'visual',
        };

        // ===== INIT =====
        function init() {
            renderProjects();
            startSessionTimer();
            // Select first project
            selectProject(state.projects[0]);
        }

        function renderProjects() {
            const list = document.getElementById('projects-list');
            list.innerHTML = '';
            state.projects.forEach(p => {
                const div = document.createElement('div');
                div.className = 'project-item' + (state.activeProject && state.activeProject.id === p.id ? ' active' : '');
                div.innerHTML = `<span class="project-color" style="background:${p.color}"></span>
      <span class="project-name">${p.name}</span>
      <span class="project-count">${p.media.length}</span>`;
                div.onclick = () => selectProject(p);
                list.appendChild(div);
            });
        }

        function selectProject(p) {
            state.activeProject = p;
            renderProjects();
            document.getElementById('topbar-info').textContent = p.name + ' · ' + p.media.length + ' 个素材';
            document.getElementById('project-panel-title').textContent = p.name;
            renderMediaGrid(p.media);
            updateContextChips();
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
            const navLabels = { agent: 'Agent 对话', project: '项目素材', splice: '视频拼接', reverse: '逆向提示词' };
            document.querySelectorAll('.nav-item').forEach(n => {
                if (n.textContent.trim().startsWith(navLabels[tab].slice(0, 3))) n.classList.add('active');
            });
        }

        // ===== MEDIA GRID =====
        const TYPE_ICONS = { video: '🎬', image: '🖼', audio: '🎵' };
        const TYPE_COLORS = { video: '#3a3a3a', image: '#003a5a', audio: '#3a2a00' };

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
                html += `<div class="media-card ${isSelected ? 'selected' : ''}" onclick="selectMedia(${m.id})" ondblclick="openMediaDetail(${m.id})">
      <div class="media-thumb">
        <div class="media-thumb-inner" style="background:${TYPE_COLORS[m.type]}22;">
          <span style="font-size:1.5rem">${TYPE_ICONS[m.type]}</span>
          <span>${m.type.toUpperCase()}</span>
        </div>
        <span class="thumb-type-badge ${m.type}">${m.type}</span>
        ${m.dur ? `<span class="thumb-duration">${m.dur}</span>` : ''}
      </div>
      <div class="media-check">${isSelected ? '✓' : ''}</div>
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
            renderMediaGrid(state.activeProject.media);
        }

        function openMediaDetail(id) {
            const m = state.activeProject.media.find(x => x.id === id);
            if (!m) return;
            state.selectedMedia = m;
            renderMediaGrid(state.activeProject.media);
            const detail = document.getElementById('media-detail');
            detail.style.display = 'flex';
            document.getElementById('detail-filename').textContent = m.name;
            document.getElementById('detail-preview').textContent = (m.type === 'video' ? '▶ VIDEO PREVIEW' : m.type === 'audio' ? '♪ AUDIO' : '◼ IMAGE PREVIEW');
            document.getElementById('detail-preview').style.background = TYPE_COLORS[m.type] || '#111';
            const meta = document.getElementById('detail-meta');
            let rows = [
                ['类型', m.type.toUpperCase()],
                ['文件名', m.name],
                ['大小', m.size || '—'],
            ];
            if (m.dur) rows.push(['时长', m.dur]);
            if (m.res) rows.push(['分辨率', m.res]);
            meta.innerHTML = rows.map(([k, v]) => `<div class="detail-row"><span class="detail-key">${k}</span><span class="detail-val">${v}</span></div>`).join('');
        }

        function closeMediaDetail() {
            document.getElementById('media-detail').style.display = 'none';
        }

        function filterMedia(type, btn) {
            document.querySelectorAll('.filter-tab').forEach(t => t.classList.remove('active'));
            btn.classList.add('active');
            if (!state.activeProject) return;
            const media = type === 'all' ? state.activeProject.media : state.activeProject.media.filter(m => m.type === type);
            renderMediaGrid(media);
        }

        function sortMedia() {
            if (!state.activeProject) return;
            state.activeProject.media.sort((a, b) => a.name.localeCompare(b.name));
            renderMediaGrid(state.activeProject.media);
        }

        function handleFileUpload(e) {
            if (!state.activeProject) { alert('请先选择项目'); return; }
            const files = Array.from(e.target.files);
            files.forEach((f, i) => {
                const type = f.type.startsWith('video') ? 'video' : f.type.startsWith('image') ? 'image' : 'audio';
                const id = Date.now() + i;
                state.activeProject.media.push({ id, name: f.name, type, size: formatSize(f.size) });
            });
            renderMediaGrid(state.activeProject.media);
            renderProjects();
            document.getElementById('topbar-info').textContent = state.activeProject.name + ' · ' + state.activeProject.media.length + ' 个素材';
        }

        function formatSize(bytes) {
            if (bytes > 1e9) return (bytes / 1e9).toFixed(1) + 'GB';
            if (bytes > 1e6) return (bytes / 1e6).toFixed(0) + 'MB';
            return (bytes / 1e3).toFixed(0) + 'KB';
        }

        function addToTimeline() {
            if (!state.selectedMedia) return;
            const m = state.selectedMedia;
            if (state.timeline.find(t => t.id === m.id)) { return; }
            state.timeline.push({ ...m, width: 120 + Math.random() * 80 | 0 });
            renderTimeline();
        }

        function deleteSelected() {
            if (!state.selectedMedia || !state.activeProject) return;
            state.activeProject.media = state.activeProject.media.filter(m => m.id !== state.selectedMedia.id);
            state.selectedMedia = null;
            closeMediaDetail();
            renderMediaGrid(state.activeProject.media);
            renderProjects();
        }

        function analyzeSelected() {
            if (!state.selectedMedia) return;
            switchTab('reverse');
            document.getElementById('rev-desc').value = `分析素材：${state.selectedMedia.name}\n类型：${state.selectedMedia.type}\n${state.selectedMedia.res ? '分辨率：' + state.selectedMedia.res : ''}`;
        }

        // ===== TIMELINE =====
        function renderTimeline() {
            const row = document.getElementById('tl-video-row');
            const tray = document.getElementById('tray-items');
            const totalEl = document.getElementById('tray-total-dur');

            if (!state.timeline.length) {
                row.innerHTML = `<div class="tl-add-zone" onclick="switchTab('project')">+ 从素材库添加</div>`;
                tray.innerHTML = '';
                totalEl.textContent = '0s';
                document.getElementById('preview-placeholder').style.display = 'flex';
                document.getElementById('preview-controls').style.display = 'none';
                return;
            }

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
            document.getElementById('preview-placeholder').style.display = 'none';
            document.getElementById('preview-controls').style.display = 'flex';
            document.getElementById('preview-time').textContent = '00:00 / ' + formatDuration(totalSecs);
        }

        function parseDur(s) {
            if (!s) return 10;
            const parts = s.split(':');
            if (parts.length === 2) return parseInt(parts[0]) * 60 + parseInt(parts[1]);
            return parseInt(s) || 10;
        }
        function formatDuration(secs) {
            const m = Math.floor(secs / 60), s = secs % 60;
            return m + ':' + String(s).padStart(2, '0');
        }

        function selectClip(i) {
            document.querySelectorAll('.tl-clip').forEach(c => c.classList.remove('selected'));
            const el = document.getElementById('clip-' + i);
            if (el) el.classList.add('selected');
        }

        function removeClip(i) {
            state.timeline.splice(i, 1);
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

        let isPlaying = false;
        function previewToggle() {
            isPlaying = !isPlaying;
            document.getElementById('play-btn').textContent = isPlaying ? '⏸ 暂停' : '▶ 播放';
            if (isPlaying) {
                setTimeout(() => { isPlaying = false; document.getElementById('play-btn').textContent = '▶ 播放'; }, 3000);
            }
        }
        function seekPrev() { showToast('跳转到上一片段'); }
        function seekNext() { showToast('跳转到下一片段'); }

        function exportVideo() {
            if (!state.timeline.length) { showToast('请先添加视频片段'); return; }
            const res = document.getElementById('res-select').value;
            showToast(`正在准备导出 ${res} 视频（演示模式）`);
        }

        // ===== AGENT CHAT =====
        const AGENT_RESPONSES = {
            default: [
                '已收到你的需求。基于当前项目素材，我建议使用慢动作切换配合环境音效，能有效增强沉浸感。\n\n我可以为你生成具体的分镜脚本或提示词，请告诉我目标平台（如 Sora、Runway、剪映）。',
                '这是个很好的创意方向！从素材分析来看，你的素材色调偏暖，建议搭配同色系 BGM 和字幕设计，整体风格会更统一。\n\n需要我生成完整的视频制作方案吗？',
                '根据你描述的场景，推荐以下分镜结构：\n1. 广角建立镜头（5s）\n2. 中景人物/主体（8s）\n3. 特写细节（3s）\n4. 回归广角收尾（5s）\n\n每个分镜我都可以生成对应的 AI 提示词。',
                '明白了。这类内容在 Sora 平台表现最佳，建议提示词中包含：\n· 具体的光线描述（如"golden hour backlight"）\n· 镜头运动描述（如"slow dolly in"）\n· 情绪关键词（如"cinematic, nostalgic"）\n\n要我生成完整提示词吗？',
            ],
            '分析当前项目的素材风格': '正在分析「' + (state.activeProject ? state.activeProject.name : '当前项目') + '」素材...\n\n素材风格分析报告：\n✓ 色调：暖色系为主，金黄色+橙色占70%\n✓ 拍摄手法：以固定机位延时为主，少量手持\n✓ 分辨率：4K高质量，适合商业输出\n✓ 时长：平均单段时长约1.5分钟\n\n建议剪辑风格：电影感慢节奏，配合管弦乐BGM',
            '帮我生成一段30秒的短片脚本': '30秒短片脚本（城市夜景主题）：\n\n[00:00-00:05] 建立镜头\n城市夜空全景，霓虹灯逐渐亮起\n\n[00:05-00:12] 主体推进\n慢推镜头，聚焦繁忙的十字路口\n\n[00:12-00:22] 细节刻画\n交替剪辑：车灯轨迹、行人剪影、招牌倒影\n\n[00:22-00:28] 情绪升华\n延时加速，城市生命力爆发\n\n[00:28-00:30] 收尾定格\n回归城市轮廓，文字标题渐显',
            '推荐适合婚礼纪录片的运镜方式': '婚礼纪录片运镜推荐：\n\n📷 仪式阶段\n· 长焦守候拍摄（不打扰主角）\n· 缓慢横移跟随步伐\n· 适当仰拍表达庄重感\n\n🎊 庆典阶段\n· 环绕移动增加动感\n· 手持轻微抖动体现真实感\n· 大景深虚化烘托氛围\n\n💑 人像阶段\n· 浅景深突出主体\n· 侧光+逆光增加情感厚度\n· 缓慢推进表达情绪',
        };

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

        function sendChatMessage() {
            const input = document.getElementById('chat-input');
            const text = input.value.trim();
            if (!text) return;
            input.value = '';

            appendMessage('user', text);
            state.chatMsgs++;

            // Show typing
            const typingId = 'typing-' + Date.now();
            appendMessageRaw(`<div class="msg agent" id="${typingId}">
    <div class="msg-meta">FrameOS Agent · 正在输入</div>
    <div class="msg-bubble agent-typing"><span class="cursor"></span></div>
  </div>`);

            const delay = 800 + Math.random() * 600;
            setTimeout(() => {
                const el = document.getElementById(typingId);
                if (el) el.remove();
                const resp = AGENT_RESPONSES[text] || AGENT_RESPONSES.default[Math.floor(Math.random() * AGENT_RESPONSES.default.length)];
                appendMessage('agent', resp);
                state.chatMsgs++;
                updateStats();
            }, delay);
        }

        function appendMessage(role, text) {
            const msgs = document.getElementById('chat-messages');
            const div = document.createElement('div');
            div.className = 'msg ' + role;
            const time = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
            div.innerHTML = `
    <div class="msg-meta">${role === 'user' ? '你' : 'FrameOS Agent'} · ${time}</div>
    <div class="msg-bubble">${escHtml(text)}</div>
    ${role === 'agent' ? `<div class="msg-actions">
      <button class="msg-action-btn" onclick="sendPreset('继续详细说明')">继续说明</button>
      <button class="msg-action-btn" onclick="copyText(this)">复制</button>
    </div>` : ''}
  `;
            msgs.appendChild(div);
            msgs.scrollTop = msgs.scrollHeight;
        }

        function appendMessageRaw(html) {
            const msgs = document.getElementById('chat-messages');
            msgs.insertAdjacentHTML('beforeend', html);
            msgs.scrollTop = msgs.scrollHeight;
        }

        function escHtml(s) { return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\n/g, '<br>'); }

        function clearChat() {
            document.getElementById('chat-messages').innerHTML = '';
            state.chatMsgs = 0;
            updateStats();
        }

        function exportChat() { showToast('对话已复制到剪贴板（演示）'); }
        function copyText(btn) { showToast('已复制'); }

        function updateStats() {
            document.getElementById('stat-msgs').textContent = state.chatMsgs;
            document.getElementById('stat-tokens').textContent = '~' + (state.chatMsgs * 180 + Math.random() * 50 | 0);
        }

        // ===== REVERSE =====
        const SAMPLE_JSON = {
            metadata: {
                title: "城市夜景延时",
                style: "写实纪录",
                platform: "Sora (OpenAI)",
                total_scenes: 5,
                generated_at: new Date().toISOString().slice(0, 19)
            },
            scenes: [
                {
                    scene_id: "S01",
                    timecode: "00:00 - 00:08",
                    type: "establishing",
                    description: "城市夜晚全景建立镜头，远山轮廓与霓虹灯光交织",
                    prompt: "Wide establishing shot of city skyline at night, neon lights reflecting on wet streets, slow dolly movement, cinematic color grading, 4K, golden hour transition",
                    camera: { movement: "缓慢推进（Slow dolly in）", angle: "广角俯瞰", lens: "24mm广角" },
                    dialogue: null,
                    mood: "宏大、沉静"
                },
                {
                    scene_id: "S02",
                    timecode: "00:08 - 00:18",
                    type: "detail",
                    description: "车流光轨特写，延时摄影展现城市节奏",
                    prompt: "Close-up time-lapse of traffic light trails on urban highway, long exposure effect, warm amber and white streaks, static tripod shot, night photography aesthetic",
                    camera: { movement: "固定机位（Static）", angle: "低角度侧拍", lens: "50mm标准" },
                    dialogue: null,
                    mood: "动感、现代"
                },
                {
                    scene_id: "S03",
                    timecode: "00:18 - 00:26",
                    type: "human",
                    description: "夜市行人剪影，人群熙攘的烟火气",
                    prompt: "Silhouette of people walking through night market, warm backlit lanterns, shallow depth of field bokeh, handheld slight movement, documentary style, vibrant street colors",
                    camera: { movement: "手持微跟随（Handheld follow）", angle: "平视人眼高度", lens: "85mm人像" },
                    dialogue: "（城市的脉搏，从未停歇。）",
                    mood: "温暖、生活气息"
                },
                {
                    scene_id: "S04",
                    timecode: "00:26 - 00:33",
                    type: "abstract",
                    description: "霓虹灯招牌反射，雨后积水倒影",
                    prompt: "Abstract reflection of neon signs in rain puddle, distorted colorful light patterns, macro shot, slow zoom out revealing the street context, impressionistic style",
                    camera: { movement: "缓慢拉出（Slow zoom out）", angle: "极低角度", lens: "微距100mm" },
                    dialogue: null,
                    mood: "梦幻、诗意"
                },
                {
                    scene_id: "S05",
                    timecode: "00:33 - 00:40",
                    type: "closing",
                    description: "回归城市全景，天空渐亮，城市进入新的一天",
                    prompt: "City skyline time-lapse, night to dawn transition, stars fading as morning glow appears on horizon, wide angle static shot, epic cinematic scale, hopeful atmosphere",
                    camera: { movement: "固定机位延时（Static time-lapse）", angle: "广角高点", lens: "16mm超广" },
                    dialogue: "（这座城市，永远在前行。）",
                    mood: "希望、升华"
                }
            ]
        };

        function analyzeVideo() {
            const desc = document.getElementById('rev-desc').value.trim();
            if (!desc) { document.getElementById('rev-desc').focus(); return; }

            const btn = document.getElementById('analyze-btn');
            btn.disabled = true;
            btn.textContent = '分析中...';
            document.getElementById('rev-status').textContent = '正在生成...';
            document.getElementById('json-status').className = 'json-status processing';
            document.getElementById('json-status').textContent = '处理中';

            setTimeout(() => {
                btn.disabled = false;
                btn.textContent = '生成分镜 JSON →';
                document.getElementById('rev-status').textContent = '已生成 · ' + new Date().toLocaleTimeString();
                document.getElementById('json-status').className = 'json-status ready';
                document.getElementById('json-status').textContent = '已完成';

                // Update metadata
                const style = document.getElementById('rev-style').value;
                const platform = document.getElementById('rev-platform').value;
                const scenes = parseInt(document.getElementById('rev-scenes').value);
                const data = { ...SAMPLE_JSON, metadata: { ...SAMPLE_JSON.metadata, style, platform, total_scenes: scenes, generated_at: new Date().toISOString().slice(0, 19) } };
                data.scenes = data.scenes.slice(0, Math.min(scenes, data.scenes.length));
                state.jsonData = data;

                renderJsonVisual(data);
                renderJsonRaw(data);
            }, 1800);
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
            renderProjects();
            selectProject(proj);
            closeModal();
            document.getElementById('new-proj-name').value = '';
            document.getElementById('new-proj-desc').value = '';
            switchTab('project');
        }

        // ===== CONTEXT CHIPS =====
        function updateContextChips() {
            const el = document.getElementById('context-chips');
            if (!state.activeProject) return;
            el.innerHTML = `<div class="context-chip">
    <span class="chip-color" style="background:${state.activeProject.color}"></span>
    <span style="font-size:0.6875rem">${state.activeProject.name}</span>
    <span class="chip-x">✕</span>
  </div>
  <div class="context-chip" style="font-size:0.6rem;color:var(--muted);">
    ${state.activeProject.media.length} 个素材
  </div>`;
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
            t.style.cssText = `position:fixed;bottom:1.5rem;right:1.5rem;background:var(--fg);color:var(--bg);font-family:var(--font-mono);font-size:0.6875rem;letter-spacing:0.04em;padding:0.5rem 0.875rem;border-radius:var(--radius);z-index:999;opacity:0;transition:opacity 0.2s;pointer-events:none;`;
            t.textContent = msg;
            document.body.appendChild(t);
            requestAnimationFrame(() => { t.style.opacity = '1'; });
            setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 200); }, 2200);
        }

        // ===== MODAL CLOSE ON BACKDROP =====
        document.getElementById('modal-overlay').addEventListener('click', function (e) {
            if (e.target === this) closeModal();
        });

        // ===== OUTPUT OPTS TOGGLE =====
        document.querySelectorAll('.output-opt').forEach(opt => {
            opt.addEventListener('click', () => opt.classList.toggle('active'));
        });

        // ===== INIT =====
        init();
    