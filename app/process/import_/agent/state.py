import copy
from typing import TypedDict


class ImportGraphState(TypedDict):

    # 传入的数据
    task_id: str  # 导入任务唯一 ID（由上传接口生成，用于追踪任务状态）
    local_file_path: str  # 上传文件落盘后的本地完整路径

    # 文件类型标识
    md_path : str  # Markdown 文件的路径（PDF 转 MD 或直接上传 MD 后使用）
    is_md_read_enabled : bool  # 是否启用 MD 文件读取（仅读取 MD 时置 True）

    pdf_path: str  # PDF 文件的路径（需转 MD 时使用）
    is_pdf_read_enabled: bool  # 是否启用 PDF 文件读取（解析 PDF 流转 MD 时置 True）

    file_title : str  # 文件名（取自上传文件 basename，去掉路径）

    # 输出文件地址
    local_dir : str  # 本次任务的工作目录（存储中间及最终生成文件的文件夹）

    md_content: str  # MD 文件内容（读取/转换后的完整文本）

    chunks : list  # 切割后的文本子块 dict（最小检索单元，用于向量化召回）

    parent_chunks : list  # 章节级父块 dict（检索命中后回填父级背景上下文）

    item_name: str  # 主体/品目名，缺省时用 file_title 兜底

    # 业务政策元数据
    document_kind: str  # 文档类型：manual（手册）/ policy（业务政策）
    document_version: str  # 政策版本号，policy 类型必填
    valid_until: float  # 政策失效时间戳（秒级），policy 类型必须晚于当前时间

    embeddings_content: list  # 已生成向量的切块（带向量字段的 chunks 子集）


# 准备一个state对象
default_state:ImportGraphState ={
    "task_id": "",
    "local_file_path": "",
    "md_path": "",
    "is_md_read_enabled": False,
    "pdf_path": "",
    "is_pdf_read_enabled": False,
    "file_title": "",
    "local_dir": "",
    "md_content": "",
    "chunks": [],
    "parent_chunks": [],
    "item_name": "",
    "document_kind": "manual",
    "document_version": "",
    "valid_until": 0.0,
    "embeddings_content": []
}

# 定义一个可以更新对象属性的函数.并且返回更新后的对象
def create_default_state(**kwargs) -> ImportGraphState:
    """
      创建一个对象! 更新指定的属性 形参列表中指定要更新的属性
      **kwargs = local_file_path = value  -> dict {local_file_path : key}
    :return:
    """
    # 赋值对象内容! 进行复制(copy)内容更新!!
    # 深拷贝 : 不仅拷贝第一层属性,也会copy嵌套属性  新的 = copy.deepcopy(对象)
    # 浅拷贝 : 仅拷贝第一层属性,嵌套属性依然共享  copy.copy  |  dict.copy()
    new_state = copy.deepcopy(default_state)
    # default_state 全局唯一对象! 多次更新,值进行共享!
    new_state.update(kwargs)
    return new_state