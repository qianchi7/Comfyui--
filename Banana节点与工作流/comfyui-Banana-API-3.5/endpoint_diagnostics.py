from __future__ import annotations

import locale
import os
import platform
import re
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse


class EndpointDiagnostics:
    """封装 Base URL 的端点诊断能力，当前主要提供 ping 测试。"""

    PING_COUNT_MIN = 1
    PING_COUNT_MAX = 10
    PING_TIMEOUT_MS = 1500

    def __init__(self, logger_instance, interrupt_checker=None) -> None:
        self.logger = logger_instance
        self.interrupt_checker = interrupt_checker

    def _ensure_not_interrupted(self) -> None:
        if self.interrupt_checker is not None:
            self.interrupt_checker()

    @classmethod
    def resolve_ping_count(cls, ping_count: Any) -> int:
        try:
            value = int(ping_count)
        except (TypeError, ValueError):
            value = 4
        return max(cls.PING_COUNT_MIN, min(value, cls.PING_COUNT_MAX))

    @staticmethod
    def extract_host_from_base_url(api_base_url: str) -> Optional[str]:
        raw_url = (api_base_url or "").strip()
        if not raw_url:
            return None

        parsed = urlparse(raw_url if "://" in raw_url else f"http://{raw_url}")
        host = parsed.hostname or ""
        host = host.strip("[]")
        return host or None

    @staticmethod
    def find_ping_executable() -> Optional[str]:
        ping_path = shutil.which("ping")
        if ping_path:
            return ping_path

        if os.name == "nt":
            windir = os.environ.get("WINDIR", r"C:\Windows")
            candidate_paths = [
                os.path.join(windir, "System32", "PING.EXE"),
                os.path.join(windir, "System32", "ping.exe"),
                os.path.join(windir, "Sysnative", "PING.EXE"),
                os.path.join(windir, "Sysnative", "ping.exe"),
            ]
            for candidate in candidate_paths:
                if os.path.exists(candidate):
                    return candidate

        return None

    @classmethod
    def build_ping_command(cls, host: str, ping_count: int) -> List[str]:
        ping_executable = cls.find_ping_executable() or "ping"
        system_name = platform.system().lower()
        if os.name == "nt":
            return [ping_executable, "-n", str(ping_count), "-w", str(cls.PING_TIMEOUT_MS), host]
        if system_name == "darwin":
            return [ping_executable, "-c", str(ping_count), "-W", str(cls.PING_TIMEOUT_MS), host]
        unix_timeout = max(1, int((cls.PING_TIMEOUT_MS + 999) / 1000))
        return [ping_executable, "-c", str(ping_count), "-W", str(unix_timeout), host]

    @staticmethod
    def parse_ping_statistics(output_text: str) -> Dict[str, Optional[float]]:
        normalized = (output_text or "").replace("\r", "\n")
        normalized = normalized.replace("，", ",").replace("：", ":").replace("％", "%")

        stats: Dict[str, Optional[float]] = {
            "sent": None,
            "received": None,
            "lost": None,
            "loss_rate": None,
            "min_ms": None,
            "avg_ms": None,
            "max_ms": None,
        }

        windows_match = re.search(
            r"Sent\s*=\s*(\d+).*?Received\s*=\s*(\d+).*?Lost\s*=\s*(\d+)",
            normalized,
            re.IGNORECASE | re.DOTALL,
        )
        if windows_match is None:
            windows_match = re.search(
                r"已发送\s*=\s*(\d+).*?已接收\s*=\s*(\d+).*?丢失\s*=\s*(\d+)",
                normalized,
                re.DOTALL,
            )
        if windows_match is not None:
            sent = int(windows_match.group(1))
            received = int(windows_match.group(2))
            lost = int(windows_match.group(3))
            stats["sent"] = float(sent)
            stats["received"] = float(received)
            stats["lost"] = float(lost)
            stats["loss_rate"] = (lost / sent * 100.0) if sent > 0 else None
        else:
            unix_match = re.search(
                r"(\d+)\s+packets transmitted,\s+(\d+)\s+(?:packets\s+)?received.*?(\d+(?:\.\d+)?)%\s+packet loss",
                normalized,
                re.IGNORECASE | re.DOTALL,
            )
            if unix_match is not None:
                sent = int(unix_match.group(1))
                received = int(unix_match.group(2))
                loss_rate = float(unix_match.group(3))
                stats["sent"] = float(sent)
                stats["received"] = float(received)
                stats["lost"] = float(max(0, sent - received))
                stats["loss_rate"] = loss_rate

        windows_rtt = re.search(
            r"Minimum\s*=\s*(\d+)\s*ms.*?Maximum\s*=\s*(\d+)\s*ms.*?Average\s*=\s*(\d+)\s*ms",
            normalized,
            re.IGNORECASE | re.DOTALL,
        )
        if windows_rtt is None:
            windows_rtt = re.search(
                r"最短\s*=\s*(\d+)\s*ms.*?最长\s*=\s*(\d+)\s*ms.*?平均\s*=\s*(\d+)\s*ms",
                normalized,
                re.DOTALL,
            )
        if windows_rtt is not None:
            stats["min_ms"] = float(windows_rtt.group(1))
            stats["max_ms"] = float(windows_rtt.group(2))
            stats["avg_ms"] = float(windows_rtt.group(3))
        else:
            unix_rtt = re.search(
                r"(?:round-trip|rtt)[^=]*=\s*([\d.]+)/([\d.]+)/([\d.]+)/",
                normalized,
                re.IGNORECASE,
            )
            if unix_rtt is not None:
                stats["min_ms"] = float(unix_rtt.group(1))
                stats["avg_ms"] = float(unix_rtt.group(2))
                stats["max_ms"] = float(unix_rtt.group(3))

        return stats

    @staticmethod
    def parse_reply_times_ms(output_text: str) -> List[float]:
        normalized = (output_text or "").replace("\r", "\n")
        normalized = normalized.replace("，", ",").replace("：", ":").replace("＜", "<")

        values: List[float] = []
        for pattern in (
            r"time[=<]\s*(\d+(?:\.\d+)?)\s*ms",
            r"时间[=<]\s*(\d+(?:\.\d+)?)\s*ms",
            r"时间[=<]\s*(\d+(?:\.\d+)?)\s*毫秒",
        ):
            matches = re.findall(pattern, normalized, re.IGNORECASE)
            if matches:
                values.extend(float(item) for item in matches)

        # 兼容 Windows 常见的 "time<1ms" / "时间<1ms" 输出
        less_than_one_matches = re.findall(r"(?:time|时间)\s*<\s*1\s*(?:ms|毫秒)", normalized, re.IGNORECASE)
        values.extend(0.5 for _ in less_than_one_matches)
        return values

    def run_base_url_ping_diagnostics(self, api_base_url: str, ping_count: Any) -> str:
        host = self.extract_host_from_base_url(api_base_url)
        if not host:
            return "🌐 Base URL 诊断: 无法从当前 API Base URL 中解析主机名"

        ping_executable = self.find_ping_executable()
        if ping_executable is None:
            return f"🌐 Base URL 诊断: 系统未找到 ping 命令，无法测试 {host} 的延迟和丢包率"

        resolved_count = self.resolve_ping_count(ping_count)
        command = self.build_ping_command(host, resolved_count)
        timeout_seconds = max(5, resolved_count * 2 + 2)
        try:
            self._ensure_not_interrupted()
            result = subprocess.run(
                command,
                capture_output=True,
                text=False,
                timeout=timeout_seconds,
                check=False,
            )
            self._ensure_not_interrupted()
        except subprocess.TimeoutExpired:
            return f"🌐 Base URL 诊断: ping {host} 超时，可能网络较差、目标禁 ping 或链路异常"
        except Exception as exc:
            return f"🌐 Base URL 诊断: ping {host} 失败（{type(exc).__name__}: {exc}）"

        output_text = self.decode_ping_output(result.stdout, result.stderr)
        stats = self.parse_ping_statistics(output_text)
        reply_times_ms = self.parse_reply_times_ms(output_text)

        if stats.get("received") is None and reply_times_ms:
            sent_count = float(resolved_count)
            received_count = float(len(reply_times_ms))
            lost_count = float(max(0, resolved_count - len(reply_times_ms)))
            stats["sent"] = sent_count
            stats["received"] = received_count
            stats["lost"] = lost_count
            stats["loss_rate"] = (lost_count / sent_count * 100.0) if sent_count > 0 else None

        if stats.get("avg_ms") is None and reply_times_ms:
            stats["min_ms"] = min(reply_times_ms)
            stats["avg_ms"] = sum(reply_times_ms) / len(reply_times_ms)
            stats["max_ms"] = max(reply_times_ms)

        parts = [f"🌐 Base URL 诊断: host={host}", f"ping={resolved_count}次"]

        sent = stats.get("sent")
        received = stats.get("received")
        if sent is not None and received is not None:
            parts.append(f"收发={int(received)}/{int(sent)}")

        loss_rate = stats.get("loss_rate")
        if loss_rate is not None:
            parts.append(f"丢包率={loss_rate:.1f}%")

        min_ms = stats.get("min_ms")
        avg_ms = stats.get("avg_ms")
        max_ms = stats.get("max_ms")
        if min_ms is not None and avg_ms is not None and max_ms is not None:
            parts.append(f"延迟 min/avg/max={min_ms:.0f}/{avg_ms:.0f}/{max_ms:.0f} ms")

        if len(parts) <= 2:
            if result.returncode == 0:
                preview = " | ".join(
                    line.strip()
                    for line in output_text.splitlines()
                    if line.strip()
                )[:180]
                if preview:
                    parts.append(f"ping 已完成，但未能解析统计信息，原始输出: {preview}")
                else:
                    parts.append("ping 已完成，但未能解析统计信息")
            else:
                parts.append(f"ping 失败，退出码={result.returncode}")

        return " | ".join(parts)

    @staticmethod
    def decode_ping_output(stdout_bytes: Optional[bytes], stderr_bytes: Optional[bytes]) -> str:
        chunks = [chunk for chunk in (stdout_bytes, stderr_bytes) if chunk]
        if not chunks:
            return ""

        merged = b"\n".join(chunks)
        for encoding in EndpointDiagnostics._candidate_ping_encodings():
            try:
                text = merged.decode(encoding)
                if text:
                    return text
            except Exception:
                continue

        return merged.decode("utf-8", errors="ignore")

    @staticmethod
    def _candidate_ping_encodings() -> List[str]:
        candidates: List[str] = []

        def add(value: Optional[str]) -> None:
            if value and value not in candidates:
                candidates.append(value)

        add(locale.getpreferredencoding(False))
        add(getattr(sys, "getfilesystemencoding", lambda: None)())

        if os.name == "nt":
            add("mbcs")
            add("oem")
            add("cp936")
            add("gbk")
            add("cp950")
            add("cp437")

        add("utf-8")
        add("latin-1")
        return candidates


__all__ = ["EndpointDiagnostics"]
