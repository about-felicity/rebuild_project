"""抖音分享链解析与无水印视频下载（供 CLI / Gradio / 后端服务复用）。"""

from __future__ import annotations

import json
import re
from pathlib import Path

import requests

__all__ = [
    "HEADER",
    "DouyinVideoDownloader",
    "download_from_share",
    "download_video",
    "extract_fst_url",
    "get_video_url",
]

HEADER = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) EdgiOS/121.0.2277.107 "
        "Version/17.0 Mobile/15E148 Safari/604.1"
    )
}


def extract_fst_url(text: str) -> str | None:
    match = re.search(r'(https?://[^\s"]+)', text)
    return match.group(1) if match else None


def _normalize_play_url(url: str) -> str:
    return (url or "").strip().replace("playwm", "play")


def _urls_from_video_block(video: dict) -> list[str]:
    out: list[str] = []
    if not isinstance(video, dict):
        return out
    for key in ("play_addr", "play_addr_h264", "play_addr_lowbr"):
        block = video.get(key)
        if isinstance(block, dict):
            ul = block.get("url_list")
            if isinstance(ul, list):
                out.extend(str(u) for u in ul if u)
    for br in video.get("bit_rate") or []:
        if not isinstance(br, dict):
            continue
        block = br.get("play_addr")
        if isinstance(block, dict) and isinstance(block.get("url_list"), list):
            out.extend(str(u) for u in block["url_list"] if u)
    return out


def _item_to_play_url(item: dict) -> tuple[str, str] | None:
    if not isinstance(item, dict):
        return None
    vid = str(item.get("aweme_id") or item.get("id") or "").strip()
    video = item.get("video")
    if not isinstance(video, dict):
        return None
    urls = _urls_from_video_block(video)
    if not urls:
        return None
    return _normalize_play_url(urls[0]), vid


def _from_video_info_res(vir: dict) -> tuple[str, str] | None:
    if not isinstance(vir, dict):
        return None
    items = vir.get("item_list")
    if isinstance(items, list) and items:
        hit = _item_to_play_url(items[0])
        if hit:
            return hit
    for key in ("aweme_detail", "aweme", "itemInfo", "item"):
        node = vir.get(key)
        if isinstance(node, dict):
            hit = _item_to_play_url(node)
            if hit:
                return hit
    return None


def _deep_collect_play_pairs(obj: object, depth: int = 0) -> list[tuple[str, str]]:
    """在整棵 JSON 里找带 aweme_id + video.play_addr 的节点。"""
    if depth > 28:
        return []
    out: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        aid = str(obj.get("aweme_id") or obj.get("id") or "").strip()
        video = obj.get("video")
        if isinstance(video, dict) and (obj.get("aweme_id") is not None or "play_addr" in video):
            for u in _urls_from_video_block(video):
                nu = _normalize_play_url(u)
                if nu.startswith("http"):
                    out.append((nu, aid))
        for v in obj.values():
            out.extend(_deep_collect_play_pairs(v, depth + 1))
    elif isinstance(obj, list):
        for x in obj:
            out.extend(_deep_collect_play_pairs(x, depth + 1))
    return out


def _resolve_from_router_json(json_data: dict, fallback_video_id: str) -> tuple[str, str] | None:
    loader = json_data.get("loaderData")
    if isinstance(loader, dict):
        for key in ("video_(id)/page",):
            page = loader.get(key)
            if isinstance(page, dict):
                vir = page.get("videoInfoRes")
                if isinstance(vir, dict):
                    hit = _from_video_info_res(vir)
                    if hit:
                        u, vid = hit
                        return u, vid or fallback_video_id
        for page in loader.values():
            if not isinstance(page, dict):
                continue
            vir = page.get("videoInfoRes")
            if isinstance(vir, dict):
                hit = _from_video_info_res(vir)
                if hit:
                    u, vid = hit
                    return u, vid or fallback_video_id
    candidates = _deep_collect_play_pairs(json_data)
    if not candidates:
        return None

    def score(t: tuple[str, str]) -> int:
        url = t[0].lower()
        s = 0
        if ".mp4" in url or "/aweme/" in url or "vod" in url or "byte" in url:
            s += 5
        if "playwm" in t[0]:
            s -= 1
        return s

    candidates.sort(key=score, reverse=True)
    u, vid = candidates[0]
    return u, vid or fallback_video_id


def _regex_play_url_from_html(html: str) -> str | None:
    patterns = (
        r'"url_list"\s*:\s*\[\s*"((?:https?:)?\\/\\/[^"\\]+)"',
        r'"url_list"\s*:\s*\[\s*"(https?://[^"]+)"',
        r'(https://[^"\'\s<>]+(?:aweme|vod|bytecdn|douyinvod)[^"\'\s<>]+)',
        r'(https://[^"\'\s<>]+\.mp4[^"\'\s<>]*)',
    )
    for pat in patterns:
        m = re.search(pat, html, re.I)
        if not m:
            continue
        u = m.group(1).replace(r"\/", "/")
        if u.startswith("http"):
            return u
    return None


def get_video_url(share_url: str) -> tuple[str, str]:
    response = requests.get(share_url, headers=HEADER, timeout=30, allow_redirects=True)
    response.raise_for_status()
    video_id = response.url.split("?")[0].rstrip("/").split("/")[-1]
    page_url = f"https://www.iesdouyin.com/share/video/{video_id}"
    response = requests.get(page_url, headers=HEADER, timeout=30, allow_redirects=True)
    response.raise_for_status()
    html = response.text

    pattern = re.compile(
        r"window\._ROUTER_DATA\s*=\s*(.*?)</script>",
        flags=re.DOTALL,
    )
    find_res = pattern.search(html)
    if not find_res or not find_res.group(1):
        raise ValueError("parse video json info from html fail (no _ROUTER_DATA)")

    raw = find_res.group(1).strip()
    try:
        json_data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"parse _ROUTER_DATA json fail: {e}") from e

    resolved = _resolve_from_router_json(json_data, video_id)
    if resolved:
        return resolved

    reg = _regex_play_url_from_html(html)
    if reg:
        return _normalize_play_url(reg), video_id

    raise ValueError(
        "无法在页面中解析视频地址（item_list 等字段可能已调整）。可换一条分享链接或稍后再试。"
    )


def download_video(video_url: str, save_path: str | Path) -> str:
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    response = requests.get(video_url, headers=HEADER, stream=True, timeout=60)
    response.raise_for_status()
    with open(save_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
    return str(save_path.resolve())


def download_from_share(share_info: str, output_dir: str | Path = "downloads") -> str:
    """
    从分享全文或短链解析并下载 MP4，返回保存的绝对路径。
    """
    if not (share_info or "").strip():
        raise ValueError("视频分享链接为空")

    share_url = extract_fst_url(share_info)
    if not share_url:
        raise ValueError("请输入有效的视频分享链接（需包含 http(s) 地址）")

    video_url, video_id = get_video_url(share_url)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{video_id}.mp4"
    return download_video(video_url, path)


class DouyinVideoDownloader:
    """
    解析抖音短链或含链接的分享全文，将无水印 MP4 保存到指定目录。

    后端用法示例::

        dl = DouyinVideoDownloader(storage_dir="/var/app/videos")
        path = dl.download("https://v.douyin.com/xxxx/")

        # 或每次显式指定目录（先目录、后链接）::
        dl = DouyinVideoDownloader()
        path = dl.download_to("/tmp/exports", share_link_or_full_text)
    """

    def __init__(self, storage_dir: str | Path | None = None):
        self._default_storage: Path | None = None
        if storage_dir is not None:
            self._default_storage = Path(storage_dir).expanduser().resolve()

    def download(
        self,
        share_link: str,
        storage_dir: str | Path | None = None,
    ) -> str:
        """
        :param share_link: 抖音短链，或包含 ``https://`` 的整段分享口令
        :param storage_dir: 保存目录；省略则使用构造时的 ``storage_dir``
        :returns: 已保存文件的绝对路径（``{video_id}.mp4``）
        :raises ValueError: 链接无效或未设置保存目录
        """
        if storage_dir is not None:
            base = Path(storage_dir).expanduser().resolve()
        elif self._default_storage is not None:
            base = self._default_storage
        else:
            raise ValueError(
                "请传入 storage_dir，或在 DouyinVideoDownloader(storage_dir=...) 中设置默认保存目录"
            )
        return download_from_share(share_link, base)

    def download_to(self, storage_dir: str | Path, share_link: str) -> str:
        """按「保存目录 → 链接」顺序调用，便于与 HTTP 接口参数顺序一致。"""
        return self.download(share_link, storage_dir=storage_dir)
