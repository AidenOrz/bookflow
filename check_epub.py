import zipfile, re, sys
import xml.etree.ElementTree as ET

p = sys.argv[1]
z = zipfile.ZipFile(p)
names = z.namelist()
bad = 0
for n in names:
    if n.endswith((".xhtml", ".opf", ".ncx", ".xml")):
        try:
            ET.fromstring(z.read(n))
        except Exception as e:
            print("XML错", n, e)
            bad += 1
opf = z.read("content.opf").decode()
for h in re.findall(r'<item[^>]*href="([^"]+)"', opf):
    # href 可能带 #fragment（ch001.xhtml#sec2），比对文件名前先剥掉
    f = h.split("#", 1)[0]
    if f and f not in names and not f.startswith(("http:", "https:", "mailto:")):
        print("缺失文件", f)
        bad += 1
ncx = z.read("toc.ncx").decode()
m = re.search(r'content="(\d+)"', ncx)
print("条目 %d | spine %d | navPoint %d | XML错误 %d"
      % (len(names), len(re.findall(r"<itemref", opf)),
         len(re.findall(r"<navPoint", ncx)), bad))
print("封面:", "cover.jpg" in names, "| mimetype 首位:", names[0] == "mimetype")
txt = "".join(z.read(n).decode("utf-8", "ignore") for n in names if n.endswith(".xhtml"))
print("包内汉字", len(re.findall(r"[\u4e00-\u9fff]", txt)),
      "| 进人", txt.count("进人"), "| 深人", txt.count("深人"),
      "| 人门", txt.count("人门"), "| ISBN", txt.count("ISBN"))
