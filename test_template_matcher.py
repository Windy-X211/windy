# -*- coding: utf-8 -*-
"""交互式测试 match_template_on_screen。

在控制台输入模板图片路径（或直接拖入文件），回车后返回匹配结果，可循环测试。
输入 q 或直接回车退出。

用法：
    & 'F:\anaconda3\python.exe' test_template_matcher.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import template_matcher
import capture


def main() -> None:
    print("=== 交互式模板匹配测试 ===")
    print("输入模板图片路径（可粘贴/拖入文件），回车返回结果；输入 q 退出。\n")

    capture.set_hide_on_capture(False)

    while True:
        try:
            raw = input("模板路径 (q 退出): ").strip().strip('"')
        except (EOFError, KeyboardInterrupt):
            print("\n退出")
            break

        if not raw or raw.lower() == "q":
            print("退出")
            break

        if not os.path.isfile(raw):
            print(f"  [错误] 文件不存在: {raw}\n")
            continue

        # 可选输入阈值
        try:
            thr_in = input("阈值 (默认 0.65，直接回车用默认): ").strip()
            threshold = float(thr_in) if thr_in else 0.65
        except (EOFError, KeyboardInterrupt):
            print("\n退出")
            break
        except ValueError:
            print("  [错误] 阈值需为数字\n")
            continue

        result = template_matcher.match_template_on_screen(raw, threshold=threshold)
        if result is not None:
            x, y, conf = result
            print(
                f"  [匹配] 归一化坐标=({x:.4f}, {y:.4f}) 置信度={conf:.3f} "
                f"(阈值 {threshold:.2f})"
            )
        else:
            print(f"  [未匹配] 置信度低于阈值 {threshold:.2f}，返回 None")
        print()


if __name__ == "__main__":
    main()
