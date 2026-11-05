"""生成分享预览图（M3.6）用的 Noto Sans SC 子集字体（D27）：GB2312 全部汉字与符号、ASCII、拉丁字母扩展、常用货币与标点。

用法（在 backend/ 下；下载的整套字体放在 data/ 里，不进 Git，用完删掉）：
  curl -L -o data/fonts-src/NotoSansSC-Bold.otf https://github.com/notofonts/noto-cjk/raw/main/Sans/SubsetOTF/SC/NotoSansSC-Bold.otf
  curl -L -o data/fonts-src/LICENSE https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/LICENSE
  uv run python tests/tools/subset_font.py data/fonts-src/NotoSansSC-Bold.otf data/fonts-src/LICENSE

输出到 app/web/fonts/：子集字体、覆盖字符表 chars.txt（渲染时据此去掉字体里没有的字，免得出现方框）与 OFL 许可。
"""

import shutil
import sys
from pathlib import Path

from fontTools import subset

OUT = Path(__file__).resolve().parents[2] / "app" / "web" / "fonts"
EXTRA = "€£¥₹₽₩₺₫฿₱₴₦₸₪×÷≈≤≥±–—‘’“”…•·→←↑↓★☆✓✗"


def charset() -> str:
    chars = set(EXTRA)
    for hi in range(0xA1, 0xF8):  # GB2312：A1–A9 为符号区，B0–F7 为一、二级汉字
        for lo in range(0xA1, 0xFF):
            try:
                chars.add(bytes([hi, lo]).decode("gb2312"))
            except UnicodeDecodeError:
                pass
    chars |= {chr(c) for c in range(0x20, 0x7F)}  # ASCII
    chars |= {chr(c) for c in range(0xA0, 0x180)}  # Latin-1 补充与拉丁扩展 A（商家名里的 é、ü 等）
    return "".join(sorted(chars))


def main(src: str, license_file: str) -> None:
    options = subset.Options()
    options.name_IDs = ["*"]
    options.notdef_outline = True
    font = subset.load_font(src, options)
    subsetter = subset.Subsetter(options)
    subsetter.populate(text=charset())
    subsetter.subset(font)
    OUT.mkdir(exist_ok=True)
    target = OUT / (Path(src).stem + "-subset.otf")
    subset.save_font(font, str(target), options)
    covered = "".join(sorted(chr(c) for c in font.getBestCmap()))
    (OUT / "chars.txt").write_text(covered, encoding="utf-8")
    shutil.copyfile(license_file, OUT / "OFL.txt")
    print(f"{target.name}: {target.stat().st_size / 1024:.0f} KB，{len(covered)} 个字符")


if __name__ == "__main__":
    main(*sys.argv[1:3])
