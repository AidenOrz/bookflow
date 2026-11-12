"""jsonio — 统一的 JSON 读写小工具。

直接写 json.load(open(...)) 在 Windows 上会长期占用文件句柄（测试里表现为
ResourceWarning，生产里表现为想删除/覆盖文件时报 PermissionError）。
这里统一用 with 包一层，顺便把缩进/非 ASCII 的设置收敛到一处。
"""
import json
import os


def read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write(path, obj, indent=None):
    """写 JSON。先落 .tmp 再 rename，避免中途崩溃留下半个文件。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
    os.replace(tmp, path)


def write_text(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
