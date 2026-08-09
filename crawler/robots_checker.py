from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser


@dataclass(frozen=True)
class RobotsDecision:
    allowed: bool
    status: str
    note: str = ""


class RobotsChecker:
    def __init__(self, fetch_text: Optional[Callable[[str], str]] = None, user_agent: str = "PortScope"):
        self.fetch_text = fetch_text
        self.user_agent = user_agent

    def check(self, url: str, configured_status: str = "未检查") -> RobotsDecision:
        if configured_status == "禁止":
            return RobotsDecision(False, "禁止", "robots 配置禁止自动访问。")
        if self.fetch_text is None:
            return RobotsDecision(configured_status == "允许", configured_status, "robots 尚未完成机器检查。")
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        try:
            text = self.fetch_text(robots_url)
            parser = RobotFileParser()
            parser.set_url(robots_url)
            parser.parse(text.splitlines())
            allowed = parser.can_fetch(self.user_agent, url)
            return RobotsDecision(allowed, "允许" if allowed else "禁止")
        except Exception as exc:
            return RobotsDecision(False, "检查失败", f"robots 检查失败：{exc}")
