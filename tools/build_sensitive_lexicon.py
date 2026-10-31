"""重建 data/sensitive_lexicon.txt（一次性工具，平时不需要跑）。

词源：<https://github.com/konsheng/Sensitive-lexicon>（MIT License）的
Get-hub 词表（``pol.txt`` 与 ``lex*.txt``）。本工具做的只是**修剪与合并**：

- 只保留四类（政治 / 极端组织 / 暴恐违禁 / 色情低俗）；``lex5``/``lex7``
  的社会新闻词组误报率高，整体不用；
- 剔除日常语境高误报的词——取舍清单在 ``tools/sensitive_stoplist.txt``
  （原始文档），本代码不含任何具体词条；
- 全库去重，行格式 ``词条<TAB>类别``，谐音/拆字变体随上游收录。

用法::

    python tools/build_sensitive_lexicon.py -o data/sensitive_lexicon.txt <词表目录>

``<词表目录>`` 下应放上游 Get-hub 词表原始文件（pol.txt、lex0..lex4.txt）。
词库改动后跑一遍 ``python tools/selfcheck.py`` 做完整性与匹配回归。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 文件名前缀 → 类别（缺的前缀归入 未分类，并在日志里提示）
SOURCE_CATEGORIES: dict[str, str] = {
    "pol": "政治敏感",
    "lex0": "政治敏感",
    "lex4": "政治敏感",
    "lex1": "极端组织",
    "lex2": "暴恐违禁",
    "lex3": "色情低俗",
}
SKIPPED_PREFIXES = ("lex5", "lex7")  # 社会新闻词组，误报率高，不收录

STOPLIST_FILE = Path(__file__).resolve().parent / "sensitive_stoplist.txt"
"""高误报剔除清单（原始文档）：[zh] / [ascii] 两个区块。"""


def load_stoplist() -> tuple[set[str], set[str]]:
    """读取剔除清单，返回 ``(中文剔除集, ASCII 剔除集)``。

    清单缺失时不剔任何词（保持词表全量），仅提示日志。
    """
    zh: set[str] = set()
    ascii_: set[str] = set()
    section: str | None = None
    try:
        lines = STOPLIST_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        print(f"  提示: 未找到剔除清单 {STOPLIST_FILE.name}，词表将全量收录")
        return zh, ascii_
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower() == "[zh]":
            section = "zh"
            continue
        if line.lower() == "[ascii]":
            section = "ascii"
            continue
        if section == "zh":
            zh.add(line)
        elif section == "ascii":
            ascii_.add(line.casefold())
    return zh, ascii_

HEADER = """# 敏感词精简词库（用于生图提示词本地二次筛查）
#
# 来源: https://github.com/konsheng/Sensitive-lexicon (MIT License)
# 版权归上游作者所有；此处为按插件场景修剪的子集：
#   - 只保留 政治/极端组织/暴恐/色情 四类，剔除日常高误报词
#     （取舍依据见 tools/sensitive_stoplist.txt）；
#   - 词形包含上游已收录的谐音/拆字变体；
#   - 行格式: 词条<TAB>类别，# 开头为注释。
# 如需追加自定义词，请用 sensitive.lexicon 配置附加词库文件，
# 不要直接改本文件（重跑工具会覆盖）。
#
"""

ASCII_START = (0x00, 0x7F)


def _is_ascii(word: str) -> bool:
    return all(ord(c) <= 0x7F for c in word)


def main() -> int:
    args = [a for a in sys.argv[1:]]
    if "-o" in args:
        idx = args.index("-o")
        out_path = Path(args[idx + 1])
        del args[idx : idx + 2]
    else:
        out_path = Path(__file__).resolve().parent.parent / "data" / "sensitive_lexicon.txt"
    if not args:
        print("用法: python tools/build_sensitive_lexicon.py -o <输出文件> <词表目录>")
        return 2
    src_dir = Path(args[0])
    stop_zh, stop_ascii = load_stoplist()
    print(f"  剔除清单: {STOPLIST_FILE.name} 中文 {len(stop_zh)} 条 / "
          f"ASCII {len(stop_ascii)} 条")

    seen: set[str] = set()
    body: list[str] = []
    for src in sorted(src_dir.iterdir()):
        prefix = src.stem.lower()
        if prefix.startswith(SKIPPED_PREFIXES):
            print(f"  跳过 {src.name}（误报率高的类别）")
            continue
        category = SOURCE_CATEGORIES.get(prefix)
        if category is None:
            continue
        file_lines = list(src.read_text(encoding="utf-8").splitlines())
        kept = []
        for raw in file_lines:
            word = raw.strip()
            if not word or word.startswith("#"):
                continue
            key = word.casefold()
            if key in seen:
                continue
            if word in stop_zh:
                continue
            if _is_ascii(word):
                if len(word) < 3 or key in stop_ascii:
                    continue
            seen.add(key)
            kept.append(f"{word}\t{category}")
        print(f"  {src.name} -> {category} {len(kept)} 条")
        body.append(f"# ==== {category}（{src.name}, {len(kept)} 条）====")
        body.extend(kept)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(HEADER + "\n".join(body) + "\n", encoding="utf-8")
    total = sum(1 for line in body if "\t" in line)
    print(f"写入 {out_path}，共 {total} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
