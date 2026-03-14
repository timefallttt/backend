from app.services.consistency.runtime import consistency_service
from app.services.indexing.runtime import graph_query_service, indexing_service
from app.services.workitems.service import WorkItemService


workitem_service = WorkItemService(
    indexing_service=indexing_service,
    graph_query_service=graph_query_service,
    consistency_service=consistency_service,
)
