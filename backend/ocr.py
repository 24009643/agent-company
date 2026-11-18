"""本地离线 OCR(基于 rapidocr-onnxruntime + PyMuPDF)。

用途:识别"扫描件 / 拍照件 / 图片型 PDF"以及直接上传的图片。
全程在本机 CPU 上运行、模型已打包在库内、不联网,数据不出企业。

设计要点:
- 引擎"懒加载":首次真正用到 OCR 时才初始化模型,之后复用,不拖慢启动。
- OCR 相关的第三方库(rapidocr / cv2 / fitz)都在函数内部按需导入;
  这样即使某台机器没装这些库,只要不上传扫描件/图片,程序依旧能正常跑。
"""
import numpy as np

_engine = None  # 全局单例:模型只加载一次,后续复用

# 单个扫描 PDF 最多 OCR 多少页(防止超大文件把上传请求拖死;按需调大)
MAX_OCR_PAGES = 30
# 渲染清晰度(DPI):越高越准但越慢,200 是清晰度与速度的平衡点
RENDER_DPI = 200
# 校准旋钮:单行置信度低于 CONF_MIN 的识别行直接丢弃(拍糊的数字比没有数字更危险,风险 #9);
# 被丢弃行占比超过 REVIEW_RATIO,整份文档标「待确认」交人工。
# 0.85 由实测分离带确定:乱码行(重度模糊)置信度 0.70~0.78,基本正确行 ≥0.90,空当取 0.85。
# ponytail 天花板(实测):轻度模糊的个别错字("报销"→"服销")置信度仍有 0.90+,置信度挡不住——
# RapidOCR 置信度校准偏高,真正兜底靠「待确认」人工复核;换引擎或加字典率校验是升级路径。
CONF_MIN = 0.85
REVIEW_RATIO = 0.3


def _get_engine():
    """懒加载 RapidOCR 引擎(只在第一次用到时初始化)。"""
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _engine = RapidOCR()
    return _engine


def _ocr_numpy(img):
    """对一张图片做 OCR,返回 (保留的文本, 总行数, 被丢弃的低置信行数)。
    result 每项是 [文本框坐标, 文本, 置信度];置信度 < CONF_MIN 的行不进知识库。"""
    engine = _get_engine()
    result, _ = engine(img)
    if not result:
        return "", 0, 0
    kept, low = [], 0
    for line in result:
        if len(line) >= 3 and line[1]:
            if float(line[2]) >= CONF_MIN:
                kept.append(line[1])
            else:
                low += 1
    return "\n".join(kept), len(result), low


def ocr_image_bytes(raw: bytes):
    """识别一张图片文件,返回 (文本, 总行数, 低置信行数)。"""
    import cv2
    arr = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)  # 解码为 BGR 三通道
    if img is None:
        return "", 0, 0
    return _ocr_numpy(img)


def ocr_pdf_bytes(raw: bytes, max_pages: int = MAX_OCR_PAGES):
    """把扫描版/图片型 PDF 逐页渲染成图片再逐页 OCR,返回 (文本, 总行数, 低置信行数)。"""
    import cv2
    import fitz  # PyMuPDF

    texts, total, low = [], 0, 0
    doc = fitz.open(stream=raw, filetype="pdf")
    try:
        n = min(doc.page_count, max_pages)
        for i in range(n):
            page = doc[i]
            pix = page.get_pixmap(dpi=RENDER_DPI)
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                pix.height, pix.width, pix.n
            )
            # PyMuPDF 输出按通道数处理,统一转成 OpenCV 习惯的 BGR
            if pix.n == 4:
                img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
            elif pix.n == 3:
                img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
            elif pix.n == 1:
                img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
            t, tt, lo = _ocr_numpy(img)
            total += tt; low += lo
            if t.strip():
                texts.append(t)
    finally:
        doc.close()
    return "\n".join(texts), total, low
