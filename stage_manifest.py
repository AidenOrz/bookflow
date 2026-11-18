"""记录这次运行是怎么跑出来的：把每个阶段的结果写进 manifest.json。

## 为什么要单独一层

IO-CONVENTIONS §1 I2 实测：四份 report.json 的 params 只有一个
{"conservative_fix": false, "src": "ocr_layout"}，dpi / model / device /
text_layer **一个都没记**。而 report.json 是 md 阶段整体重写的，
所以「这次是怎么跑出来的」根本无处可存。

manifest.json 记两件 report.json 都不该承担的事：
  1. **输入身份**（PDF 的 sha256 + 页数）——路径相同不代表内容相同；
  2. **每个阶段跑没跑、跑成什么样**（ok/degraded/failed/skipped）——
     「目录存在」不能当「阶段跑完」（§2 O5）。

## 三条纪律

* **不挡路**：manifest 读不出来/写不进去，都只打印一行告警，绝不 sys.exit。
  元数据文件没有资格挡住出书。
* **不猜**：读不到 PDF 页数就写 null，不拿目录里的页数冒充。
* **不改产物**：阶段函数返回的统计照样打印，manifest 只是旁路记录。
"""
import contextlib
import os
import sys
import time

import manifest as mf
import paths


def _warn(msg):
    sys.stderr.write("  ！manifest 记录失败（不影响本次运行）：%s\n" % msg)


def stage(run_root, name, fn=None, count_of=None):
    """阶段外包一层：跑之前记 running，跑完按结果记 ok/degraded/failed。

    用法：
        stats = stage(a.work, "ocr", lambda: do_work(a))

    fn 抛 SystemExit 时**照常向上抛**（那是阶段的正常失败路径），
    只在抛之前把 failed 记进 manifest——否则失败一次之后，
    状态会永远停在 running，下次打开界面显示"正在跑"。
    """
    t0 = time.time()
    try:
        mf.set_stage(run_root, name, "running")
    except Exception as e:        # noqa: BLE001 —— 记录失败不许影响主流程
        _warn(e)
    try:
        out = fn() if fn else None
    except SystemExit as e:
        try:
            mf.set_stage(run_root, name, "failed", elapsed=time.time() - t0,
                         detail=str(e.code if hasattr(e, "code") else e)[:300])
        except Exception as e2:    # noqa: BLE001
            _warn(e2)
        raise
    except BaseException as e:
        try:
            mf.set_stage(run_root, name, "failed", elapsed=time.time() - t0,
                         detail=("%s: %s" % (type(e).__name__, e))[:300])
        except Exception as e2:    # noqa: BLE001
            _warn(e2)
        raise
    # 统计和记录分开两段 try：count_of 写错不该让状态停在 running
    # （实测：统计函数抛异常时 "ok" 一直没写，界面永远显示"正在跑"）。
    cnt = None
    if count_of:
        try:
            cnt = count_of(out)
        except Exception as e:    # noqa: BLE001
            _warn("统计失败：%s" % e)
    try:
        mf.set_stage(run_root, name, "ok", count=cnt, elapsed=time.time() - t0)
    except Exception as e:        # noqa: BLE001
        _warn(e)
    return out


def note(run_root, name, status, count=None, out=None, detail="", elapsed=None):
    """直接记一条阶段结果（不需要外包时用，例如失败后要记 degraded）。"""
    try:
        mf.set_stage(run_root, name, status, count=count, out=out,
                     detail=detail, elapsed=elapsed)
    except Exception as e:        # noqa: BLE001
        _warn(e)


def inputs(run_root, pdf="", title="", author="", publisher="", pages=None):
    """记输入身份。pdf 给绝对路径，manifest 内部存相对 + sha256。"""
    try:
        mf.set_input(run_root, pdf=pdf, title=title, author=author,
                     publisher=publisher, pages=pages)
    except Exception as e:        # noqa: BLE001
        _warn(e)


def params(run_root, **kw):
    """记参数快照（值为 None 的跳过，表示"这次没给，用默认"）。"""
    try:
        mf.set_params(run_root, **kw)
    except Exception as e:        # noqa: BLE001
        _warn(e)


def check_pdf(run_root, pdf, force=False):
    """换书检测。返回 True 表示是同一本（或无法判断）。

    与 stage_textlayer 现有那条 realpath 比较**并存、不替代**：
    路径比较能立刻发现"换个文件"，内容比较能发现"同名不同版"。
    两者都过不了才提示，因为多打一行提示的代价远小于拿旧文字层配新书。
    """
    if not pdf or not os.path.isfile(pdf):
        return True
    try:
        same, old, new = mf.verify_input(run_root, pdf)
    except Exception as e:        # noqa: BLE001
        _warn(e)
        return True
    if same:
        return True
    sys.stdout.write("  提示：%s 的内容与上次记录的不同（%s -> %s）。\n"
                     "        若这本书的文字层/OCR 是在旧 PDF 上产的，请加 --force 重跑。\n"
                     % (os.path.basename(pdf), old or "?", new or "?"))
    return False


def snapshot(run_root):
    """给界面/日志用的一份完整快照。"""
    doc = mf.read(run_root)
    doc["_paths"] = paths.describe(run_root)
    doc["_progress"] = mf.progress(run_root)
    return doc


@contextlib.contextmanager
def suppress():
    """只给测试/脚本用：把 manifest 的告警吞掉（正常流程不需要）。"""
    old = sys.stderr
    devnull = open(os.devnull, "w")
    sys.stderr = devnull
    try:
        yield
    finally:
        sys.stderr = old
        devnull.close()
