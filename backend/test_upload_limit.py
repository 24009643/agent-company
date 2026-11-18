"""Step 2.2① 自检:上传大小上限(🔴R1b DoS 边界)。

直接单测 read_capped:size 预检 + 读取有界兜底(防 Content-Length 撒谎)。不碰 Ollama。
运行:python test_upload_limit.py
"""
import asyncio
from fastapi import HTTPException
from config import MAX_UPLOAD_BYTES
from app import read_capped
from rag import parse_file


class FakeUpload:
    """仿 Starlette UploadFile:.size(来自 Content-Length,可缺可假)+ .read(n)。"""
    def __init__(self, size, data):
        self.size = size
        self._d = data

    async def read(self, n=-1):
        return self._d[:n] if n is not None and n >= 0 else self._d


def _rejected(up):
    try:
        asyncio.run(read_capped(up))
        return False
    except HTTPException as e:
        return e.status_code == 413


def main():
    # 1) size 预检:声明超限 → 413(不读内容)
    assert _rejected(FakeUpload(MAX_UPLOAD_BYTES + 1, b"")), "超限 size 应 413"

    # 2) Content-Length 撒谎(size=None 或谎报小):靠读取有界兜底 → 413
    assert _rejected(FakeUpload(None, b"x" * (MAX_UPLOAD_BYTES + 1))), "size 缺失+超大内容应 413"
    assert _rejected(FakeUpload(1, b"x" * (MAX_UPLOAD_BYTES + 1))), "谎报小 size+超大内容应 413"

    # 3) 正常小文件 → 原样返回
    assert asyncio.run(read_capped(FakeUpload(10, b"0123456789"))) == b"0123456789", "正常文件应放行"

    # 4) 控制字符清洗:PDF 字体子集化偶发把字形解码成 NUL/控制字符(实测某简历含 21 个 \x00),
    #    会截断 SQLite length()、污染 LLM 提示、让证据逐字回贴失败 → parse_file 出口须清除,保留 \n\r\t。
    text, needs_review = parse_file("resume.txt", "姓名\x00邮箱\x07测试\n第二行\t制表\r回车".encode("utf-8"))
    assert "\x00" not in text and "\x07" not in text, "NUL 与控制字符应被清除"
    assert text == "姓名邮箱测试\n第二行\t制表\r回车", f"仅清控制字符、保留 \\n\\r\\t: {text!r}"
    assert needs_review is False

    print("read_capped 自检通过 ✅(size 预检 + 读取有界兜底 + 控制字符清洗)")


if __name__ == "__main__":
    main()
