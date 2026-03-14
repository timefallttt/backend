from datetime import datetime
from typing import Dict, List

from app.services.workitems.schemas import (
    WorkItemCodeSeed,
    WorkItemConnectorSummary,
    WorkItemDetail,
    WorkItemSummary,
)


class DemoWorkItemConnector:
    key = "demo"

    def __init__(self) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self._connector = WorkItemConnectorSummary(
            connector_key=self.key,
            name="演示工单源",
            description="用于本地联调的占位工单接入器，企业交付时可替换为实际实现。",
            mode="demo",
        )
        self._items: Dict[str, WorkItemDetail] = {
            "wi-avatar-01": WorkItemDetail(
                connector_key=self.key,
                item_id="wi-avatar-01",
                requirement_id="R-AVATAR-01",
                title="头像上传前压缩与失败处理",
                repo_name="",
                business_tag="profile",
                priority="high",
                status="ready_for_review",
                updated_at=now,
                requirement_text="用户上传头像时，系统应先压缩图片，再执行上传，并对失败情况给出可复核提示。",
                acceptance_criteria=[
                    "图片长边压缩到不超过 1024px。",
                    "压缩后文件大小不超过 300KB。",
                    "压缩失败时提示用户，并阻断上传。",
                    "上传失败时提示用户并允许最多重试 3 次。",
                ],
                owner="产品经理A",
                notes="该工单来自既有需求系统，候选代码种子由关联提交中的文件和函数摘要生成。",
                external_url="https://example.invalid/workitems/wi-avatar-01",
                snapshot_hint="linked-commit-demo",
                candidate_seeds=[
                    WorkItemCodeSeed(
                        seed_id="seed-avatar-page",
                        filename="entry/src/main/ets/pages/ProfilePage.ets",
                        code="async onSelectAvatar(file: File) {\n  const compressed = await this.avatarService.compressImage(file)\n  await this.avatarService.uploadAvatar(compressed)\n}",
                        start_line=18,
                        end_line=24,
                        recall_reason="工单关联提交命中页面事件入口",
                    ),
                    WorkItemCodeSeed(
                        seed_id="seed-avatar-service",
                        filename="entry/src/main/ets/service/AvatarService.ets",
                        code="async uploadAvatar(file: File) {\n  return await uploadFile('/api/avatar', file)\n}",
                        start_line=6,
                        end_line=10,
                        recall_reason="工单关联提交命中头像服务",
                    ),
                ],
            ),
            "wi-order-02": WorkItemDetail(
                connector_key=self.key,
                item_id="wi-order-02",
                requirement_id="R-ORDER-02",
                title="支付失败时订单回滚校验",
                repo_name="",
                business_tag="order",
                priority="medium",
                status="ready_for_review",
                updated_at=now,
                requirement_text="用户支付失败时，订单状态必须回滚为已取消，并记录失败原因。",
                acceptance_criteria=[
                    "支付失败后订单状态应更新为已取消。",
                    "需要记录支付失败原因。",
                    "回滚逻辑需要覆盖超时和第三方错误两类失败路径。",
                ],
                owner="产品经理B",
                notes="示例工单用于验证导入后再做图证据扩展的流程。",
                external_url="https://example.invalid/workitems/wi-order-02",
                snapshot_hint="linked-commit-demo",
                candidate_seeds=[
                    WorkItemCodeSeed(
                        seed_id="seed-order-service",
                        filename="entry/src/main/ets/service/OrderService.ets",
                        code="async submitOrder(payload: OrderPayload) {\n  const payment = await this.paymentService.pay(payload)\n  if (!payment.ok) {\n    await this.cancelOrder(payload.orderId)\n  }\n}",
                        start_line=22,
                        end_line=30,
                        recall_reason="工单关联提交命中订单主流程",
                    ),
                ],
            ),
        }

    @property
    def summary(self) -> WorkItemConnectorSummary:
        return self._connector

    def list_items(self) -> List[WorkItemSummary]:
        return [
            WorkItemSummary(**item.model_dump(exclude={"requirement_text", "acceptance_criteria", "owner", "notes", "external_url", "candidate_seeds", "snapshot_hint"}))
            for item in self._items.values()
        ]

    def get_item(self, item_id: str) -> WorkItemDetail:
        item = self._items.get(item_id)
        if not item:
            raise ValueError(f"work item not found: {item_id}")
        return item
