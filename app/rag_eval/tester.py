"""
RAG 评估测试类。

这个文件故意写得很薄。
你只需要记住一个类和三个方法：

- `RagEvalTester.run_insert_test_data()`
- `RagEvalTester.run_eval()`
- `RagEvalTester.run_answer_eval()`

其他具体流程都放在 `runner.py` 里。

当前设计思路是：
- 测试知识数据尽量多一些；
- 测试问题保持少量但有代表性。
"""


class RagEvalTester:
    """
    RAG 评估统一入口类。

    最简单用法：

    ```python
    from app.rag_eval import RagEvalTester

    tester = RagEvalTester()
    tester.run_insert_test_data()
    tester.run_eval()
    ```
    """

    def run_answer_eval(self, *args, **kwargs) -> dict:
        """校验或运行离线答案评测；默认不调用外部服务。"""
        from app.rag_eval.answer_quality import run_answer_eval
        return run_answer_eval(*args, **kwargs)

    def run_insert_test_data(self) -> dict:
        """
        插入评测测试数据。

        返回值包含：
        - item_name：写入的主体名称
        - item_rows：item_name 集合查询结果
        - chunk_rows：chunks 集合查询结果
        - case_count：生成的评测用例数量
        """
        from app.rag_eval.runner import insert_batch_eval_dataset
        from app.shared.clients.mongo import close_mongo_client

        try:
            return insert_batch_eval_dataset()
        finally:
            close_mongo_client()

    def run_eval(self) -> dict:
        """
        运行批量评测。

        返回值包含：
        - eval_results：每条问题的详细评测结果
        - summary：批量汇总结果
        - report_path：评测报告文件路径
        """
        from app.rag_eval.runner import run_batch_eval
        from app.shared.clients.mongo import close_mongo_client

        try:
            return run_batch_eval()
        finally:
            close_mongo_client()
