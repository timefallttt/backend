import time
from ..data_models import SearchResponse, SearchResult

class SearchService:
    """
    搜索服务类，负责处理代码检索业务逻辑
    """
    
    async def search_code(self, query: str, top_k: int, threshold: float) -> SearchResponse:
        """
        搜索代码片段
        
        Args:
            query: 搜索查询字符串
            top_k: 返回结果数量
            threshold: 相似度阈值
            
        Returns:
            SearchResponse: 搜索结果响应
        """
        # 记录开始时间
        start_time = time.time()
        
        # 模拟搜索结果
        # 在实际应用中，这里会调用模型进行语义搜索
        results = [
            SearchResult(
                snippet_id="1",
                code="// 示例代码 1\nfunction test1() {\n  console.log('Hello World');\n}",
                filename="example1.ts",
                start_line=1,
                end_line=4,
                similarity=0.95,
                commit_message="Add test function"
            ),
            SearchResult(
                snippet_id="2",
                code="// 示例代码 2\nfunction test2() {\n  return true;\n}",
                filename="example2.ts",
                start_line=1,
                end_line=4,
                similarity=0.85,
                commit_message="Add another test function"
            ),
            SearchResult(
                snippet_id="3",
                code="// 示例代码 3\nclass Example {\n  constructor() {}\n  method() {}\n}",
                filename="example3.ts",
                start_line=1,
                end_line=5,
                similarity=0.75,
                commit_message="Add Example class"
            )
        ]
        
        # 过滤结果，只返回相似度高于阈值的
        filtered_results = [r for r in results if r.similarity >= threshold]
        
        # 限制返回数量
        limited_results = filtered_results[:top_k]
        
        # 计算响应时间
        latency_ms = (time.time() - start_time) * 1000
        
        # 构建响应
        return SearchResponse(
            latency_ms=latency_ms,
            total_found=len(limited_results),
            results=limited_results
        )