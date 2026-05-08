import { CONFIG } from "../config.js";
import { apiPost } from "./http.js";

export function getSampleStoryboardTemplate() {
    return {
        metadata: {
            title: "城市夜景延时",
            style: "写实纪录",
            platform: "Sora (OpenAI)",
            total_scenes: 5,
            generated_at: new Date().toISOString().slice(0, 19),
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
                mood: "宏大、沉静",
            },
            {
                scene_id: "S02",
                timecode: "00:08 - 00:18",
                type: "detail",
                description: "车流光轨特写，延时摄影展现城市节奏",
                prompt: "Close-up time-lapse of traffic light trails on urban highway, long exposure effect, warm amber and white streaks, static tripod shot, night photography aesthetic",
                camera: { movement: "固定机位（Static）", angle: "低角度侧拍", lens: "50mm标准" },
                dialogue: null,
                mood: "动感、现代",
            },
            {
                scene_id: "S03",
                timecode: "00:18 - 00:26",
                type: "human",
                description: "夜市行人剪影，人群熙攘的烟火气",
                prompt: "Silhouette of people walking through night market, warm backlit lanterns, shallow depth of field bokeh, handheld slight movement, documentary style, vibrant street colors",
                camera: { movement: "手持微跟随（Handheld follow）", angle: "平视人眼高度", lens: "85mm人像" },
                dialogue: "（城市的脉搏，从未停歇。）",
                mood: "温暖、生活气息",
            },
            {
                scene_id: "S04",
                timecode: "00:26 - 00:33",
                type: "abstract",
                description: "霓虹灯招牌反射，雨后积水倒影",
                prompt: "Abstract reflection of neon signs in rain puddle, distorted colorful light patterns, macro shot, slow zoom out revealing the street context, impressionistic style",
                camera: { movement: "缓慢拉出（Slow zoom out）", angle: "极低角度", lens: "微距100mm" },
                dialogue: null,
                mood: "梦幻、诗意",
            },
            {
                scene_id: "S05",
                timecode: "00:33 - 00:40",
                type: "closing",
                description: "回归城市全景，天空渐亮，城市进入新的一天",
                prompt: "City skyline time-lapse, night to dawn transition, stars fading as morning glow appears on horizon, wide angle static shot, epic cinematic scale, hopeful atmosphere",
                camera: { movement: "固定机位延时（Static time-lapse）", angle: "广角高点", lens: "16mm超广" },
                dialogue: "（这座城市，永远在前行。）",
                mood: "希望、升华",
            },
        ],
    };
}

/**
 * @param {{ description: string, style: string, platform: string, sceneCount: number }} input
 */
export async function requestStoryboardJson(input) {
    if (CONFIG.USE_MOCK_API) {
        await new Promise((r) => setTimeout(r, 1200));
        const SAMPLE_JSON = getSampleStoryboardTemplate();
        const data = {
            ...SAMPLE_JSON,
            metadata: {
                ...SAMPLE_JSON.metadata,
                style: input.style,
                platform: input.platform,
                total_scenes: input.sceneCount,
                generated_at: new Date().toISOString().slice(0, 19),
            },
        };
        data.scenes = data.scenes.slice(0, Math.min(input.sceneCount, data.scenes.length));
        return data;
    }
    return apiPost("/api/analysis/storyboard", {
        description: input.description,
        style: input.style,
        platform: input.platform,
        scene_count: input.sceneCount,
    });
}
