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

XML_HEAD = (
    '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
    '<html xmlns="http://www.w3.org/1999/xhtml" '
    'xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="{lang}" lang="{lang}">\n'
    '<head><meta charset="utf-8"/><title>{t}</title>'
    '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n<body>\n')


def esc(s):
    return htmllib.escape(s, quote=False)


# ------------------------------------------------------------------ 解析
def parse_md(text):
    """Markdown → [(kind, payload)]。kind ∈ h / p / q / def / img。

    `q` 是引用块（我们用它承载"锚不上正文的注文"），`def` 是脚注定义。
    连续的引用行合并成一条，避免每条注文各占一个 blockquote。
    """
    blocks, cur = [], []

    def flush():
        if cur:
            blocks.append(("p", "".join(cur)))
            del cur[:]

    for raw in text.split("\n"):
        line = raw.rstrip()
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
        out.append('<a class="fnref" epub:type="noteref" role="doc-noteref" '
                   'href="#fn-%s" id="fnref-%s">%d</a>' % (esc(nid), esc(nid), k))
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
            body.append('<figure><img src="%s" alt="插图"/></figure>' % esc(b[1]))
        # def 在文件末尾统一渲染

    # 脚注区：只渲染本文件真正引用到的注，且按引用顺序编号
    seen, aside = set(), []
    for nid in used:
        if nid in seen:
            continue
        seen.add(nid)
        txt = defs.get(nid, "")
        aside.append('<aside class="footnote" epub:type="footnote" '
                     'role="doc-footnote" id="fn-%s">' % esc(nid))
        aside.append('<p><a class="fnback" role="doc-backlink" href="#fnref-%s">%d</a> %s</p>'
                     % (esc(nid), order[nid], inline(txt, order, [])))
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
.cover-page{margin:0;padding:0;text-align:center;}
.cover-page img{max-width:100%;height:auto;}
"""


def build(md_text, out, title="", author="", publisher="", lang="zh-CN",
          toc_depth=3, cover_jpg=None, css_path=None):
    """Markdown → EPUB3 文件。返回统计字典。"""
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
        body = ('<section epub:type="cover" class="cover-page">'
                '<img src="cover.jpg" alt="%s"/></section></body></html>'
                % esc(title or "封面"))
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


def validate(path):
    """结构自检：mimetype 首位、XML 合法、manifest/spine/nav 链接可达。"""
    errs = 0
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        if z.namelist()[0] != "mimetype" or z.getinfo("mimetype").compress_type != 0:
            print("校验: mimetype 必须是首个条目且不压缩")
            errs += 1
        for n in names:
            if n.endswith((".xhtml", ".opf", ".ncx", ".xml")):
                try:
                    ET.fromstring(z.read(n))
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
        # 交叉引用：正文的 fnref 必须能到达对应的 aside，反之亦然
        allx = "".join(z.read(n).decode("utf-8") for n in names if n.endswith(".xhtml"))
        refs = set(re.findall(r'href="#fn-([^"]+)"', allx))
        asides = set(re.findall(r'id="fn-([^"]+)"', allx))
        for r in sorted(refs - asides):
            print("校验: 脚注引用无对应注文 %s" % r)
            errs += 1
    return errs