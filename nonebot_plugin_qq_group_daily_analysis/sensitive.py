"""本地敏感词筛查（生图链路的兜底防线，词库可选）。

背景：部分生图服务对提示词里的敏感内容会直接返回
``400 InputTextSensitiveContentDetected``，浪费配额还不给图。

本模块做**二次筛查**：词库文件（可选、自备，默认找
``data/sensitive_lexicon.txt``）+ 用户配置的附加词库
（``gdr_sensitive_lexicon``），对交给生图模型的提示词做归一化匹配——

- 归一化：NFKC 折叠全角/繁简符号、去零宽字符与 CQ 码、统一小写；
- 去分隔符：字符间插入的标点/空格/ ``～`` 等一律消除，能命中
  拆写过的词条；
- 谐音变体：以词库文件内录入的变体为准，ASCII 另做 ``@→a``、
  ``3→e``、``1→i`` 一类 leet 折叠。

具体词条只存在于词库文件（原始文档）中，代码里不收录。词库文件缺失
或为空时**静默跳过**本层筛查（仅 debug 级日志），不影响出图。

本模块只负责「报告哪里命中」，不做文本改写/删除——误删会让画面理解失真，
改写交给文字模型（见 :mod:`llm` 的改写提示词构造函数）。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from nonebot.log import logger

PACKAGED_LEXICON = Path(__file__).resolve().parent / "data" / "sensitive_lexicon.txt"
"""默认词库查找路径；词库自备、可选，文件不在时静默跳过本层筛查。"""

# 归一化时保留的字符：CJK 汉字（含扩展区）、拉丁字母、数字。其余（标点、
# 空白、符号、emoji……）整体删除——这就是「去分隔符」。
_KEEP_RE = re.compile(r"[^0-9a-z\u3007\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# CQ 码 / [指令] 这类方括号插入语，先整段剥掉再归一化
_TAG_RE = re.compile(r"\[[^\]]{0,64}]")

# 零宽字符：常见的拆字 smuggling 手段
_ZW_RE = re.compile(r"[\u200b-\u200f\u2060\u2061\ufe00-\ufe0f\ufeff]")

# ASCII leet 折叠表（对词条与文本对称应用）
_LEET = str.maketrans({
    "4": "a", "@": "a", "3": "e", "1": "i", "!": "i", "0": "o",
    "5": "s", "$": "s", "7": "t", "9": "g", "8": "b", "+": "t",
})


def _normalize(text: str) -> str:
    """归一化任意文本：小写 + 去分隔符，用于匹配。

    Args:
        text: 原始文本（可含 CQ 码、emoji、全角符号）。

    Returns:
        只剩 ``[0-9a-z]`` + 汉字的紧凑串；空文本返回空串。
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = _TAG_RE.sub("", text)
    text = _ZW_RE.sub("", text)
    # 词库折叠串只保留小写字母——这里同步 lower，词条与文本对称
    return _KEEP_RE.sub("", text.lower().translate(_LEET))


@dataclass(frozen=True, slots=True)
class SensitiveEntry:
    """词库里的一个词条。"""

    word: str
    """展示用原词。"""
    category: str
    """类别，如 政治敏感 / 色情低俗。"""
    folded: str
    """匹配用 归一化+leet 折叠后的串。"""


@dataclass(frozen=True, slots=True)
class SensitiveHit:
    """一次命中的记录。"""

    word: str
    category: str


def load_entries(paths: Sequence[Path], *, packaged_first: bool = False) -> list[SensitiveEntry]:
    """按顺序加载词库文件，合并去重。

    行格式：``词条\\t类别`` 或 ``词条``（类别缺省为 ``未分类``）；
    ``#`` 开头的行是注释，空行忽略。后加载词库的重复词条会被忽略。

    Args:
        paths: 词库文件路径列表，前面的优先。
        packaged_first: 首个路径视为随包分发词库——文件缺失时**静默**
            跳过（词库是可选项）；用户自行配置的附加词库缺失仍记日志。

    Returns:
        词条列表（保持词库内顺序）。
    """
    entries: list[SensitiveEntry] = []
    seen: set[str] = set()
    for i, path in enumerate(paths):
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except OSError:
            if packaged_first and i == 0:
                # 词库可选：默认路径上没有词库就静默跳过，仅 debug 留痕
                logger.debug(f"[群日报] 未找到词库 {path}，跳过本地敏感词筛查")
                continue
            logger.warning(f"[群日报] 敏感词库 {path} 无法读取，跳过该词库")
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            word, _, cat = line.partition("\t")
            word = word.strip()
            cat = (cat or "未分类").strip()
            if not word:
                continue
            folded = _normalize(word)
            if not folded or folded in seen:
                continue
            seen.add(folded)
            entries.append(SensitiveEntry(word=word, category=cat, folded=folded))
    return entries


def format_word_list(entries: Iterable[SensitiveEntry], limit: int = 6) -> str:
    """把命中词条格式化成日志友好的短列表。"""
    words = [e.word for e in entries]
    head = "、".join(words[:limit])
    return head + (f" 等 {len(words)} 个" if len(words) > limit else "")


class SensitiveChecker:
    """敏感词匹配器。词量在数千级，对单次调用的提示词串检查足够快。

    Args:
        entries: 已加载的词条（:func:`load_entries` 的产物）。
        enabled: ``False`` 时 :meth:`scan` 恒返回空（总开关关闭）。
    """

    __slots__ = ("_entries", "_enabled")

    def __init__(self, entries: Sequence[SensitiveEntry], *, enabled: bool = True) -> None:
        self._entries = tuple(entries)
        self._enabled = enabled

    @property
    def size(self) -> int:
        """词条总数。"""
        return len(self._entries)

    @property
    def enabled(self) -> bool:
        """筛查总开关。"""
        return self._enabled

    @property
    def active(self) -> bool:
        """当前是否真的在筛（开关开着**且**有词条）。词库可选的基本判据。"""
        return self._enabled and bool(self._entries)

    @property
    def lexicon_paths(self) -> list[str]:
        """已加载的词库文件（仅打包，附加词库路径不在此列）。"""
        return [str(PACKAGED_LEXICON)]

    def scan(self, text: str) -> list[SensitiveHit]:
        """扫描文本，返回全部命中（词条级别去重）。

        Args:
            text: 待检文本（生图 scene、分镜提示词等）。

        Returns:
            命中的 :class:`SensitiveHit` 列表，按文件顺序；无命中返回 ``[]``。
        """
        if not text or not self._enabled:
            return []
        normalized = _normalize(text)
        if not normalized:
            return []
        hits: list[SensitiveHit] = []
        for entry in self._entries:
            if len(entry.folded) > len(normalized):
                continue
            if entry.folded in normalized:
                hits.append(SensitiveHit(word=entry.word, category=entry.category))
        return hits

    def is_sensitive(self, text: str) -> bool:
        """命中任何词条即 ``True``。"""
        return bool(self.scan(text))


_CHECKER_CACHE: dict[str, "SensitiveChecker"] = {}


def get_checker(config) -> SensitiveChecker:
    """按配置返回缓存的筛查器。

    词库文件在插件生命周期内只读一次；词库实际改了可
    ``sensitive._CHECKER_CACHE.clear()`` 后再取。

    Args:
        config: 插件配置（``Config``，避免循环导入用鸭子类型）。

    Returns:
        :class:`SensitiveChecker`。
    """
    extra: list[Path] = []
    raw = getattr(config, "gdr_sensitive_lexicon", "") or ""
    for item in str(raw).split(","):
        item = item.strip()
        if item:
            extra.append(Path(item))
    enabled = bool(getattr(config, "gdr_sensitive_filter", True))
    lex_paths: list[Path] = [PACKAGED_LEXICON, *extra]
    cache_key = repr((lex_paths, enabled))
    cached = _CHECKER_CACHE.get(cache_key)
    if cached is None:
        checker = SensitiveChecker(
            load_entries(lex_paths, packaged_first=True), enabled=enabled
        )
        if checker.active:
            logger.info(
                f"[群日报] 敏感词筛查已加载 {checker.size} 个词条"
                f"（{len(lex_paths)} 个词库文件，启用中）"
            )
        else:
            # 词库可选：一个词条都没有就当作关闭，静默降级
            logger.debug("[群日报] 未加载到任何敏感词词条，跳过本地敏感词筛查")
        cached = checker
        _CHECKER_CACHE[cache_key] = cached
    return cached
