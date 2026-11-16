"""stdio — 让 CLI 脚本的中文输出在 Windows 上不再炸掉。

## 为什么需要这个模块（实测事故）

Windows 上子进程的 stdout 默认编码是 **GBK（cp936）**，不是 UTF-8。只要某一行输出
里出现 GBK 编不进去的字符，`print()` 就会抛 `UnicodeEncodeError`，脚本当场死掉：

    UnicodeEncodeError: 'gbk' codec can't encode character '\xe4' in position 72

实测出处：跑《拉康式主体》（上海文艺，2024）时 `repair.py` 崩在半路。它把补识别
的对照行打给人看（`认出%2.0f/应有%2.0f | 旧文本 -> 新文本`），而这一页的正文里有
德语 Übertragung 的 **Ü**、法语 Écrits 的 **É**、ça 的 **ç**、数学符号 ∅ ∀ ∃、
下标 S₁ S₂ —— 全都不在 GBK 里。全书 155 行含这类字符。

这个崩溃点的恶劣之处：**它在业务逻辑已经跑完之后才炸**。repair.py 已经把
`ocr_fix` 写到第 129 页，崩在打印上，于是 `bookflow.py all` 看到退出码 1，
判定整个 repair 阶段失败，**其余 181 页一个都没写**。用户看到的是"识别失败"，
实际上识别全都成功了，只是话说不出口。

## 为什么不用 PYTHONIOENCODING

设环境变量能治，但那是**调用方的责任**，而调用方（bookflow.py）自己都不设。
本项目的头号风险是"静默出错"，让每个新脚本都记得配一个环境变量正是那种会漏的约定。
所以由脚本自己 reconfigure：谁要打印，谁负责把话说明白。

## 为什么不干脆全用 ASCII

机器可读的行（如 textlayer.py 的 `SUMMARY {...}`）确实该纯 ASCII，那是**接口契约**，
由各自模块保证（见 textlayer.summary_line）。但 `repair.py` 那种对照行是**给人看的**，
转义成 `\u00dc` 之后人就不认识了——那不是修复，是把问题挪到人眼这一侧。

所以分两层：**机器读的走 ASCII，人读的走 UTF-8**，各有各的保证。
"""
import sys


def utf8_stdio():
    """把本进程的 stdout/stderr 强制成 UTF-8，写不出的字符退化成 U+FFFD。

    `errors="replace"` 是第二道保险：即使调用方拿不走 UTF-8（例如 `chcp 936`
    的老终端），也只是把那个字显示成 `?`，**进程不会死**。把"显示不了"降级成
    乱码/问号，而不是让整轮流程失败——这是刻意的取舍。

    被重定向成不可 reconfigure 的对象（测试里的 StringIO）时静默跳过。
    """
    for name in ("stdout", "stderr"):
        s = getattr(sys, name, None)
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass          # 没有 reconfigure，或流已关闭——都不是我们能修的
