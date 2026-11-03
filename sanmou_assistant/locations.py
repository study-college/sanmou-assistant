"""Game shared addresses, kept separate from screenshot pixel positions."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SharedAddress:
    name: str
    x: int
    y: int

    def text(self) -> str:
        return f"{self.name}({self.x},{self.y})"


def parse_addresses(text: str) -> list[SharedAddress]:
    """Parse the whole paste or reject it; never silently import a valid prefix."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("请粘贴游戏地址，例如：涪县(595,277)")
    if len(text) > 10000:
        raise ValueError("一次粘贴的地址过多，请分批添加")
    source = text.translate(str.maketrans({"（": "(", "）": ")", "，": ","}))
    matches = list(re.finditer(r"\(\s*(\d{1,4})\s*,\s*(\d{1,4})\s*\)", source))
    if not matches:
        raise ValueError("地址格式不正确，请使用 名称(X,Y)")
    result, seen, end = [], {}, 0
    separators = " \t\r\n,;；、"
    for match in matches:
        name = text[end:match.start()].strip(separators + "，")
        normalized = name.translate(str.maketrans({"（":"(","）":")"}))
        depth = 0
        for char in normalized:
            depth += (char == "(") - (char == ")")
            if depth < 0:
                break
        if len(name) > 80 or any(c in name for c in "\r\n\x00") or depth != 0 or re.search(r"\([^)]*\d[^)]*[,，]", normalized):
            raise ValueError("地址名称或括号不完整，请检查整段粘贴内容")
        x, y = int(match[1]), int(match[2])
        if not (0 <= x <= 3000 and 0 <= y <= 3000):
            raise ValueError(f"坐标 ({x},{y}) 超出 0～3000 范围")
        coord = x, y
        if coord in seen and seen[coord] != name:
            raise ValueError(f"坐标 ({x},{y}) 对应多个名称，请核对后添加")
        if coord not in seen:
            result.append(SharedAddress(name, x, y))
            seen[coord] = name
        end = match.end()
    if source[end:].strip(separators):
        raise ValueError("地址末尾有未识别内容，请检查名称和括号是否完整")
    return result


def single_address(text: str) -> SharedAddress:
    addresses = parse_addresses(text)
    if len(addresses) != 1:
        raise ValueError("此处只设置一个地点，请只粘贴一个地址")
    return addresses[0]
