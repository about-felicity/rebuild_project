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


def get_video_url(share_url: str) -> tuple[str, str]:
    response = requests.get(share_url, headers=HEADER, timeout=30)
    response.raise_for_status()
    video_id = response.url.split("?")[0].strip("/").split("/")[-1]
    page_url = f"https://www.iesdouyin.com/share/video/{video_id}"
    response = requests.get(page_url, headers=HEADER, timeout=30)
    response.raise_for_status()

    pattern = re.compile(
        r"window\._ROUTER_DATA\s*=\s*(.*?)</script>",
        flags=re.DOTALL,
    )
    find_res = pattern.search(response.text)
    if not find_res or not find_res.group(1):
        raise ValueError("parse video json info from html fail")

    json_data = json.loads(find_res.group(1).strip())
    data = json_data["loaderData"]["video_(id)/page"]["videoInfoRes"]["item_list"][0]
    video_url = data["video"]["play_addr"]["url_list"][0].replace("playwm", "play")
    return video_url, video_id


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
