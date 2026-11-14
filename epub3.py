# -*- coding: utf-8 -*-
"""bookflow.epub3 — 项目自带的 Markdown → EPUB3 构建器（含弹窗脚注）。

为什么自己写而不用现成的 pandoc / md-to-ebook：

1. **弹窗脚注需要 EPUB3 语义标记**：正文里必须是
   `<a epub:type="noteref" role="doc-noteref">`，注文必须是
   `<aside epub:type="footnote" role="doc-footnote">`。Apple Books、Thorium、
   Kobo 都靠这对标记弹出注文并跳回原处。普通的 `<sup>[1]</sup>` 链接不会弹窗。
2. **章级切分要按本书的标题层级**，不能按 Markdown 的 `#` 数量猜——我们的
   层级是印刷目录给的（讲/章 = 1 级，节 = 2 级，一、= 3 级）。
3. **封面要按本书的裁切逻辑**（cover.py 的墨迹外接矩形），而不是取最大插图。

支持的 Markdown 子集 = bookflow 自己会产出的那些：标题、段落、引用块、
脚注引用 `[^id]` 与定义、图片。多余语法一律按纯文本处理，不猜。
"""
import os
import re
import html as htmllib
import posixpath
import uuid
import zipfile
import datetime
import xml.etree.ElementTree as ET

CJK = re.compile(r"[\u4e00-\u9fff]")
HEAD = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
FNDEF = re.compile(r"^\[\^([^\]]+)\]:\s*(.*)$")
FNREF = re.compile(r"\[\^([^\]]+)\]")
QUOTE = re.compile(r"^>\s?(.*)$")
MD_IMG = re.compile(r"^!\[[^\]]*\]\(([^)\s]+)\)\s*$")
HTML_IMG = re.compile(r"^<img[^>]*src=\"([^\"]+)\"[^>]*>\s*$", re.I)
# epub:type 属性在 ElementTree 里是「命名空间 + 本地名」的形式
OPS_TYPE = "{http://www.idpf.org/2007/ops}type"

XML_HEAD = (
    '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
    '<html xmlns="http://www.w3.org/1999/xhtml" '
    'xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="{lang}" lang="{lang}">\n'
    '<head><meta charset="utf-8"/><title>{t}</title>'
    '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n<body>\n')


def esc(s):
    """文本节点转义：不转双引号。

    正文文本节点必须走这里。quote=False 是有意的——正文里出现 `"` 时不
    该变成 `&quot;`，否则全量 EPUB 的字节都会变（我们重建时正是靠字节比对
    确认没有回归）。
    """
    return htmllib.escape(s, quote=False)


def esc_attr(s):
    """属性值转义：补上双引号（属性一律用 " 包裹）。

    为什么必须有这个函数：esc() 用 quote=False，不转 `"`。它被用在
    href= / id= / src= / alt= 这些**属性**位置时，注号或书名里只要有一个
    `"` 就会提前闭合属性，产出非法 XML（实测：`[^a"b]` 的注号、
    `书名"引号` 的书名都能让整本 EPUB 解析失败）。

    只补属性必需的 `& < > "` 四个字符，不动 `'`——属性用双引号包裹，
    单引号无需转义；少转一个字符，重建前后的 EPUB 也能尽量保持字节一致。
    """
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ------------------------------------------------------------------ 解析
def parse_md(text):
    """Markdown → [(kind, payload)]。kind ∈ h / p / q / def / img / tbl。

    `q` 是引用块（我们用它承载"锚不上正文的注文"），`def` 是脚注定义。
    连续的引用行合并成一条，避免每条注文各占一个 blockquote。
    `tbl` 是中文表格区（layout 用三反引号 + table 标注包起来），渲染成
    `<pre class="table-block">`；围栏内的空行属于表格行距，不能当分段符。
    """
    blocks, cur = [], []

    def flush():
        if cur:
            blocks.append(("p", "".join(cur)))
            del cur[:]

    in_tbl = [False]
    for raw in text.split("\n"):
        line = raw.rstrip()
        # ```table 围栏：layout 把表格区的文字整块放在这里。
        # 围栏内的空行不能 flush（那是表格的行间距），所以要在通用的
        # 「空行即分段」判断**之前**处理。围栏外的一切照旧。
        if line.strip().startswith("```"):
            if in_tbl[0]:
                in_tbl[0] = False
                blocks.append(("tbl", "\n".join(cur)))
                del cur[:]
            elif line.strip() == "```table":
                flush()
                in_tbl[0] = True
            else:
                # 别的围栏我们不认识：按普通文本收，不猜。
                cur.append(line)
            continue
        if in_tbl[0]:
            cur.append(line)
            continue
        if not line.strip():
            flush()
            continue
        m = FNDEF.match(line)
        if m:
            flush()
            blocks.append(("def", m.group(1), m.group(2).strip()))
            continue
        m = HEAD.match(line)
        if m:
            flush()
            blocks.append(("h", len(m.group(1)), m.group(2).strip()))
            continue
        m = MD_IMG.match(line) or HTML_IMG.match(line)
        if m:
            flush()
            blocks.append(("img", m.group(1)))
            continue
        m = QUOTE.match(line)
        if m:
            flush()
            blocks.append(("q", m.group(1).strip()))
            continue
        cur.append(line)
    if in_tbl[0]:
        # 围栏没闭合（md 被截断 / 手改坏了）：把已收到的内容原样落成表格块。
        # 直接丢会静默吞掉这一段——那正是这次要修的病。
        blocks.append(("tbl", "\n".join(cur)))
        del cur[:]
    flush()
    return blocks


def inline(t, order, note_ids):
    """行内渲染：转义 + 脚注引用。

    `order` 是本文件内的脚注编号表（id → 序号），`note_ids` 收集本文件用到的
    脚注（用于在文件末尾生成 aside）。编号按**出现顺序**重排成 1,2,3…——
    原书的圈号①②③在正文里已经被替换掉了，读者看到的是干净的上标数字。
    """
    out, last = [], 0
    for m in FNREF.finditer(t):
        nid = m.group(1)
        out.append(esc(t[last:m.start()]))
        if nid not in order:
            order[nid] = len(order) + 1
        k = order[nid]
        note_ids.append(nid)
        # href / id 都是属性位，注号可能含 "（如 [^a"b]），必须走 esc_attr
        out.append('<a class="fnref" epub:type="noteref" role="doc-noteref" '
                   'href="#fn-%s" id="fnref-%s">%d</a>'
                   % (esc_attr(nid), esc_attr(nid), k))
        last = m.end()
    out.append(esc(t[last:]))
    s = "".join(out)
    # 少量行内强调：我们自己的产物里不会出现，但手改过的 md 可能有
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    return s


# ------------------------------------------------------------------ 切章
def split_chapters(blocks):
    """按 1 级标题切章；章前的散块并入第一章之前（前言页）。"""
    chapters, cur = [], {"title": "", "blocks": []}
    for b in blocks:
        if b[0] == "h" and b[1] == 1:
            if cur["blocks"] or cur["title"]:
                chapters.append(cur)
            cur = {"title": b[2], "blocks": []}
            continue
        cur["blocks"].append(b)
    if cur["blocks"] or cur["title"]:
        chapters.append(cur)
    return chapters


def render_chapter(ch, fid, lang, toc_depth):
    """一章 → (xhtml, nav 子项, 用到的脚注数)。"""
    order, body, nav = {}, [], []
    defs = {b[1]: b[2] for b in ch["blocks"] if b[0] == "def"}
    used = []
    hcount = [0]

    if ch["title"]:
        body.append('<h1 class="chapter-title">%s</h1>' % esc(ch["title"]))
    for b in ch["blocks"]:
        k = b[0]
        if k == "h":
            lvl = min(max(b[1], 2), 6)
            hcount[0] += 1
            aid = "h%d_%d" % (fid, hcount[0])
            body.append('<h%d id="%s">%s</h%d>' % (lvl, aid, inline(b[2], order, used), lvl))
            if b[1] <= toc_depth:
                nav.append((aid, b[2], b[1]))
        elif k == "p":
            body.append('<p>%s</p>' % inline(b[1], order, used))
        elif k == "q":
            # 锚不上正文的注：扫描件的上标注号常被 OCR 漏掉，这些注在正文里
            # 找不到可点的位置。仍须保留全文（学术书的注就是正文的一部分），
            # 但只能集中排在章末，用 epub:type="endnote" 与弹窗脚注区分开。
            body.append('<aside class="endnote" epub:type="endnote" role="doc-endnote">'
                        '<p>%s</p></aside>' % inline(b[1], order, used))
        elif k == "img":
            # src 是属性位；图片名可能含 " 或 &，必须 esc_attr
            body.append('<figure><img src="%s" alt="插图"/></figure>' % esc_attr(b[1]))
        elif k == "tbl":
            # 表格区：整块原文放进 <pre class="table-block">。
            # 为什么是 pre 而不是 <table>：我们没有列对齐证据，硬拼成表格等于
            # 凭空造结构；pre 保留原样的列边界（行内全角空格）且等宽对齐，
            # 读者能看出这是张表，我们又没伪造任何东西。
            body.append('<pre class="table-block">%s</pre>'
                        % esc(b[1].strip("\n")))
        # def 在文件末尾统一渲染

    # 脚注区：只渲染本文件真正引用到的注，且按引用顺序编号
    seen, aside = set(), []
    for nid in used:
        if nid in seen:
            continue
        seen.add(nid)
        txt = defs.get(nid, "")
        # id / href 都是属性位，与正文里的 fnref 必须用同一套转义，否则注号含 "
        # 时两边会一对都坏掉
        aside.append('<aside class="footnote" epub:type="footnote" '
                     'role="doc-footnote" id="fn-%s">' % esc_attr(nid))
        aside.append('<p><a class="fnback" role="doc-backlink" href="#fnref-%s">%d</a> %s</p>'
                     % (esc_attr(nid), order[nid], inline(txt, order, [])))
        aside.append("</aside>")
    if aside:
        body.append('<section class="footnotes" epub:type="footnotes">')
        body.extend(aside)
        body.append("</section>")

    x = (XML_HEAD.format(t=esc(ch["title"] or "正文"), lang=lang)
         + '<section epub:type="chapter">\n' + "\n".join(body)
         + "\n</section>\n</body>\n</html>\n")
    return x, nav, len(seen)


DEFAULT_CSS = """@charset "utf-8";
body{margin:0 5%;line-height:1.75;text-align:justify;
 font-family:"Source Han Serif SC","Noto Serif CJK SC","Songti SC","SimSun",serif;color:#000;}
h1.chapter-title{font-size:1.5em;text-align:center;margin:2.2em 0 1.6em;line-height:1.45;}
h2{font-size:1.22em;margin:2em 0 .7em;line-height:1.45;}
h3{font-size:1.06em;margin:1.6em 0 .5em;line-height:1.5;}
p{margin:.42em 0;text-indent:2em;}
/* 弹窗脚注：正文里的上标引用 */
a.fnref{text-decoration:none;font-size:.72em;vertical-align:super;line-height:0;
 padding:0 .1em;color:#8A2D2D;}
/* 注文区：Apple Books / Thorium 会按 aside[epub:type=footnote] 弹窗 */
section.footnotes{margin-top:2.2em;border-top:1px solid #999;padding-top:.6em;font-size:.86em;}
aside.footnote{margin:.5em 0;line-height:1.55;}
aside.footnote p{text-indent:0;margin:.3em 0;}
a.fnback{text-decoration:none;color:#8A2D2D;margin-right:.35em;}
/* 锚不上正文的注：作为页尾注保留全文 */
aside.endnote{display:block;font-size:.86em;color:#333;margin:.3em 0 .3em 1.2em;}
aside.endnote p{text-indent:0;margin:.2em 0;}
figure{margin:1.2em auto;text-align:center;page-break-inside:avoid;break-inside:avoid;}
figure img{max-width:100%;height:auto;}
/* 表格区：不重建结构，只保证「不拆碎」。pre 保留原样列边界，等宽对齐。 */
pre.table-block{font-family:"Source Han Serif SC","Noto Serif CJK SC","Songti SC",serif;
 font-size:.86em;line-height:1.5;margin:1em 0;padding:.5em .6em;
 white-space:pre-wrap;word-break:break-all;background:#f7f7f7;
 border-left:3px solid #ccc;page-break-inside:avoid;break-inside:avoid;}
.cover-page{margin:0;padding:0;text-align:center;}
.cover-page img{max-width:100%;height:auto;}
"""


def find_images(blocks, base_dir):
    """md 里引用到的图片 → {包内文件名: 磁盘路径}。

    只认相对路径（`images/fig-p143-1.png`），不认 http/data URI——那些不是
    包内资源，硬拷进来反而会造出坏包。找不到的图**不在这里报错**：
    validate() 会逐条报「图片不存在」，而 build 不该因为缺一张图就整体失败。

    base_dir 是 md 所在目录。不是「当前工作目录」：make_epub.py 从别处调用时
    cwd 与 md 所在目录常常不是同一个，用 cwd 会一整天找不到图。
    """
    got = {}
    for b in blocks:
        if b[0] != "img":
            continue
        src = (b[1] or "").strip()
        if not src or src.startswith(("http:", "https:", "data:", "mailto:")):
            continue
        if os.path.isabs(src):
            continue
        p = os.path.normpath(os.path.join(base_dir or ".", src))
        if os.path.exists(p):
            got[src.replace(os.sep, "/")] = p
    return got


MEDIA_BY_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".svg": "image/svg+xml",
                ".webp": "image/webp"}


def build(md_text, out, title="", author="", publisher="", lang="zh-CN",
          toc_depth=3, cover_jpg=None, css_path=None, base_dir=None):
    """Markdown → EPUB3 文件。返回统计字典。

    base_dir：md 里相对图片路径的基准目录（通常是 md 所在目录）。给 None 时
    按当前工作目录解析——旧调用方不传也照旧能用。
    """
    blocks = parse_md(md_text)
    chapters = split_chapters(blocks)
    if not chapters:
        raise SystemExit("book.md 里没有任何标题，无法切章")

    manifest, spine, files = [], [], {}
    nav_items, n_notes = [], 0
    for i, ch in enumerate(chapters, 1):
        fn = "c%03d.xhtml" % i
        x, nav, nn = render_chapter(ch, i, lang, toc_depth)
        n_notes += nn
        files[fn] = x.encode("utf-8")
        manifest.append('<item id="c%03d" href="%s" media-type="application/xhtml+xml"/>' % (i, fn))
        spine.append('<itemref idref="c%03d"/>' % i)
        nav_items.append((esc(ch["title"] or "正文"), fn, nav))

    # 图片：md 里 ![](images/fig-p143-1.png) 引用的文件必须真的进包，
    # 否则成书里就是一个个碎图占位符（旧版正是这样：parse_md 认了 img，
    # build 却从没把文件塞进 files，OPF manifest 里也没有条目）。
    imgs = find_images(blocks, base_dir)
    for i, (rel, path) in enumerate(sorted(imgs.items()), 1):
        with open(path, "rb") as f:
            files[rel] = f.read()
        ext = os.path.splitext(rel)[1].lower()
        manifest.append('<item id="img%03d" href="%s" media-type="%s"/>'
                        % (i, esc_attr(rel), MEDIA_BY_EXT.get(ext, "application/octet-stream")))

    if css_path and os.path.exists(css_path):
        with open(css_path, "rb") as f:
            files["style.css"] = f.read()
    else:
        files["style.css"] = DEFAULT_CSS.encode("utf-8")
    manifest.append('<item id="css" href="style.css" media-type="text/css"/>')

    # 封面
    if cover_jpg and os.path.exists(cover_jpg):
        with open(cover_jpg, "rb") as f:
            files["cover.jpg"] = f.read()
        # alt 是属性位：书名含 " 时用 esc 会提前闭合属性，整页 XHTML 报废
        body = ('<section epub:type="cover" class="cover-page">'
                '<img src="cover.jpg" alt="%s"/></section></body></html>'
                % esc_attr(title or "封面"))
        files["cover.xhtml"] = (XML_HEAD.format(t="封面", lang=lang)
                                + body).encode("utf-8")
        manifest.append('<item id="coverpage" href="cover.xhtml" media-type="application/xhtml+xml"/>')
        manifest.append('<item id="cover-image" href="cover.jpg" media-type="image/jpeg" '
                        'properties="cover-image"/>')
        spine.insert(0, '<itemref idref="coverpage"/>')

    # nav / ncx
    def nav_html(items):
        o = ["<ol>"]
        for label, href, kids in items:
            o.append('<li><a href="%s">%s</a>' % (href, label))
            if kids:
                o.append(nav_html([(esc(k[1]), "%s#%s" % (href, k[0]), []) for k in kids]))
            o.append("</li>")
        o.append("</ol>")
        return "\n".join(o)

    nav_x = (XML_HEAD.format(t="目录", lang=lang)
             + '<nav epub:type="toc" id="toc"><h1>目录</h1>\n'
             + nav_html(nav_items) + "\n</nav>\n"
             + '<nav epub:type="landmarks" id="landmarks" hidden="hidden"><ol>'
               '<li><a epub:type="bodymatter" href="%s">正文</a></li>' % nav_items[0][1]
             + ('<li><a epub:type="cover" href="cover.xhtml">封面</a></li>'
                if "cover.jpg" in files else "")
             + "</ol></nav></body></html>")
    files["nav.xhtml"] = nav_x.encode("utf-8")

    cnt = [0]

    def ncx_tree(items):
        o = []
        for label, href, kids in items:
            cnt[0] += 1
            o.append('<navPoint id="np%d" playOrder="%d"><navLabel><text>%s</text></navLabel>'
                     '<content src="%s"/>' % (cnt[0], cnt[0], label, href))
            if kids:
                o.extend(ncx_tree([(esc(k[1]), "%s#%s" % (href, k[0]), []) for k in kids]))
            o.append("</navPoint>")
        return o

    doc_uuid = "urn:uuid:" + str(uuid.uuid4())
    modified = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    files["toc.ncx"] = (('<?xml version="1.0" encoding="utf-8"?>\n'
                         '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1" '
                         'xml:lang="%s"><head><meta name="dtb:uid" content="%s"/></head>'
                         '<docTitle><text>%s</text></docTitle><navMap>%s</navMap></ncx>')
                        % (lang, doc_uuid, esc(title or "book"),
                           "".join(ncx_tree(nav_items)))).encode("utf-8")

    meta = ['<dc:identifier id="bookid">%s</dc:identifier>' % doc_uuid,
            '<dc:title>%s</dc:title>' % esc(title or "未命名"),
            '<dc:language>%s</dc:language>' % lang]
    if author:
        meta.append('<dc:creator>%s</dc:creator>' % esc(author))
    if publisher:
        meta.append('<dc:publisher>%s</dc:publisher>' % esc(publisher))
    meta.append('<meta property="dcterms:modified">%s</meta>' % modified)
    if "cover.jpg" in files:
        meta.append('<meta name="cover" content="cover-image"/>')
    files["content.opf"] = (('<?xml version="1.0" encoding="utf-8"?>\n'
                            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
                            'unique-identifier="bookid" xml:lang="%s"><metadata '
                            'xmlns:dc="http://purl.org/dc/elements/1.1/">%s</metadata><manifest>'
                            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
                            'properties="nav"/>'
                            '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
                            '%s</manifest><spine toc="ncx">%s</spine></package>')
                           % (lang, "".join(meta), "".join(manifest),
                              "".join(spine))).encode("utf-8")
    files["META-INF/container.xml"] = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>').encode("utf-8")

    # 打包：mimetype 必须首个且不压缩
    tmp = out + ".tmp"
    with zipfile.ZipFile(tmp, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        for name in sorted(files):
            z.writestr(name, files[name], compress_type=zipfile.ZIP_DEFLATED)
    os.replace(tmp, out)
    return {"chapters": len(chapters), "notes": n_notes,
            "files": len(files), "nav": cnt[0]}


def _text_of(el):
    """元素内全部文字（跳过 doc-backlink 的那个编号链接）。

    aside 注文里的 <a class="fnback">1</a> 是回链编号，不是注文内容。
    不跳过它的话，注文为空（引用无定义 / 定义在别的章）的 aside 也会因为
    这个 "1" 而显得"非空"，漏检。

    tail 的拼接顺序照 ElementTree 的文档语义：子元素的 tail 属于父元素，
    所以 doc-backlink 自己的 tail 仍要收，只跳过它的 text。
    """
    buf = [el.text or ""]

    def walk(e):
        for c in e:
            if c.get("role") == "doc-backlink":
                if c.tail:
                    buf.append(c.tail)
                continue
            buf.append(c.text or "")
            walk(c)
            if c.tail:
                buf.append(c.tail)

    walk(el)
    return "".join(buf).strip()


def _target_exists(names, docs, src, base):
    """解析 href/src（相对 base 文件所在目录）并检查目标是否存在。

    返回 (ok, reason)。ok=False 时 reason 已包含具体原因，方便打印。
    带 #anchor 的还要检查锚点在目标文档里真的存在——nav/ncx 的锚点
    指向 h{章号}_{序号}，切章或 toc_depth 一改就会错位，光查文件不够。
    """
    if src.startswith(("http:", "https:", "data:", "mailto:")):
        # 外链 / data URI 不指向包内文件，这里不管（EPUB3 是否允许远程资源
        # 是另一条规则，check_epub.py 对 manifest 也是同样放行）
        return True, ""
    f, _, frag = src.partition("#")
    if f:
        path = posixpath.normpath(posixpath.join(posixpath.dirname(base), f))
    else:
        path = base
    if path not in names:
        return False, "缺文件 %s（来源 %s）" % (src, base)
    if frag:
        root = docs.get(path)
        if root is not None:
            have = set()
            for e in root.iter():
                eid = e.get("id")
                if eid:
                    have.add(eid)
            if frag not in have:
                return False, "缺锚点 %s（来源 %s）" % (src, base)
    return True, ""


def validate(path):
    """结构自检：返回错误条数（0 = 无错误）。

    查 5 类老问题（mimetype 首位、XML 合法、manifest 文件、spine idref、
    脚注引用→注文）+ 新补的 4 类漏检。这 4 类都是实测能骗过旧版的：
    1. 同一注号引用两次 [^x]…[^x]：旧版只做 set 减法，refs 与 asides 都是
       单元素集合，看不出来，但产物里会有 2 个 fnref-x（重复 id）而只有
       1 条注文。
    2. 引用无定义 [^nope]、3. 定义写在别的章：两者产物长得一样——aside
       照样生成但注文为空，旧版完全查不出。
    4. md 里写 ![](pic.png) 但包内没有这张图：旧版只查 OPF manifest，
       xhtml 里的 img src 是死链也不报。
    """
    errs = 0
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        if z.namelist()[0] != "mimetype" or z.getinfo("mimetype").compress_type != 0:
            print("校验: mimetype 必须是首个条目且不压缩")
            errs += 1
        # 全量解析一次，后面几项检查复用（文档顺序用 sorted 固定，报错稳定）。
        # 解析失败的文件不进 docs，后面按 xhtml 的逐条检查自然跳过它，
        # 避免一个坏文件连报几十条噪声。
        docs = {}
        for n in sorted(names):
            if n.endswith((".xhtml", ".opf", ".ncx", ".xml")):
                try:
                    docs[n] = ET.fromstring(z.read(n))
                except Exception as e:
                    print("校验: XML 非法 %s -> %s" % (n, e))
                    errs += 1
        opf = z.read("content.opf").decode("utf-8")
        for h in re.findall(r'<item[^>]*href="([^"]+)"', opf):
            if h.split("#")[0] not in names:
                print("校验: manifest 缺文件 %s" % h)
                errs += 1
        ids = set(re.findall(r'<item[^>]*id="([^"]+)"', opf))
        for sid in re.findall(r'<itemref idref="([^"]+)"', opf):
            if sid not in ids:
                print("校验: spine 无效 idref %s" % sid)
                errs += 1

        # ---- 以下逐 xhtml 检查；XML 已经非法的不再重复报（会连报一堆噪声）
        for n in sorted(docs):
            if not n.endswith(".xhtml"):
                continue
            root = docs[n]
            seen_ids = set()
            refs = {}           # fn-x -> 被引用的次数
            backlinks = set()   # href="#fnref-x" 指向的 id
            for el in root.iter():
                eid = el.get("id")
                if eid:
                    if eid in seen_ids:
                        # 同一个 xhtml 里 id 必须唯一；重复时阅读器跳转行为未定义
                        print("校验: id 重复 %s -> %s" % (n, eid))
                        errs += 1
                    seen_ids.add(eid)
                href = el.get("href") or ""
                if href.startswith("#fn-"):
                    refs[href[4:]] = refs.get(href[4:], 0) + 1
                elif href.startswith("#fnref-"):
                    backlinks.add(href[1:])
                if el.get(OPS_TYPE) == "footnote":
                    if not _text_of(el):
                        # 引用无定义 / 定义在别的章，都落到这里
                        print("校验: 注文为空 %s" % (el.get("id") or "(无 id)"))
                        errs += 1
                if el.tag.endswith("img"):
                    ok, msg = _target_exists(names, docs, el.get("src") or "", n)
                    if not ok:
                        print("校验: 图片不存在 %s" % msg)
                        errs += 1
            asides = {i[3:] for i in seen_ids if i.startswith("fn-")}
            for r in sorted(set(refs) - asides):
                print("校验: 脚注引用无对应注文 %s" % r)
                errs += 1
            # 反向：每条注文都必须有正文引用，否则读者在书里永远点不到它
            for a in sorted(asides - set(refs)):
                print("校验: 注文无正文引用 %s" % a)
                errs += 1
            for r, k in sorted(refs.items()):
                if k > 1:
                    # 2 条 noteref 对 1 条 aside：正文会出现重复 id=fnref-x
                    print("校验: 脚注引用重复 %d 次 %s" % (k, r))
                    errs += 1
            for b in sorted(backlinks - seen_ids):
                print("校验: 注文回链无对应引用 %s" % b)
                errs += 1

        # ---- nav.xhtml / toc.ncx 的链接必须可达
        for doc in ("nav.xhtml", "toc.ncx"):
            root = docs.get(doc)
            if root is None:
                continue
            for el in root.iter():
                if el.tag.endswith("a"):
                    src = el.get("href") or ""
                elif el.tag.endswith("content"):
                    src = el.get("src") or ""
                else:
                    continue
                if not src:
                    continue
                ok, msg = _target_exists(names, docs, src, doc)
                if not ok:
                    print("校验: 目录链接不可达 %s" % msg)
                    errs += 1
    return errs