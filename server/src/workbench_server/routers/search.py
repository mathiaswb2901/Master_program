"""Content-search REST API. Thin: the service owns the walk and the ignore rules."""

from fastapi import APIRouter, Request

from workbench_server.models.search import SearchRequest, SearchResponse
from workbench_server.services.search import SearchService

router = APIRouter(prefix="/api/search", tags=["search"])


def _service(request: Request) -> SearchService:
    service: SearchService = request.app.state.search
    return service


@router.post("")
async def search(request: Request, body: SearchRequest) -> SearchResponse:
    """Find ``body.query`` across the workspace's files, grouped by file.

    Empty ``files`` is the honest "no matches" answer; ``truncated`` says the scan
    stopped at ``max_results`` and more exist (raise it, or narrow the query).
    """
    return _service(request).search(body)
