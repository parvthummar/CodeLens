from pydantic import BaseModel


class SearchRequest(BaseModel):
    query: str
    top_k: int = 10


class SearchResult(BaseModel):
    name: str
    entity_type: str
    code: str
    signature: str
    description: str
    file_path: str
    start_line: int
    end_line: int
    score: float


class SearchResponse(BaseModel):
    results: list[SearchResult]
