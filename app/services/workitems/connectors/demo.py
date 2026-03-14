from datetime import datetime
from typing import Dict, List

from app.services.workitems.schemas import (
    WorkItemConnectorSummary,
    WorkItemDetail,
    WorkItemDiffHunk,
    WorkItemFileDiff,
    WorkItemLinkedCommit,
    WorkItemSummary,
)


class DemoWorkItemConnector:
    key = 'demo'

    def __init__(self) -> None:
        now = datetime.now().isoformat(timespec='seconds')
        self._connector = WorkItemConnectorSummary(
            connector_key=self.key,
            name='演示工单源',
            description='用于本地联调的占位工单接入器，企业交付时可替换为实际实现。',
            mode='demo',
        )
        self._items: Dict[str, WorkItemDetail] = {
            'wi-avatar-01': WorkItemDetail(
                connector_key=self.key,
                item_id='wi-avatar-01',
                requirement_id='R-AVATAR-01',
                title='头像上传前压缩与失败处理',
                repo_name='',
                business_tag='profile',
                priority='high',
                status='ready_for_review',
                updated_at=now,
                requirement_text='用户上传头像时，系统应先压缩图片，再执行上传，并对失败情况给出可复核提示。',
                acceptance_criteria=[
                    '图片长边压缩到不超过 1024px。',
                    '压缩后文件大小不超过 300KB。',
                    '压缩失败时提示用户，并阻断上传。',
                    '上传失败时提示用户并允许最多重试 3 次。',
                ],
                owner='产品经理A',
                notes='该工单关联多个提交，导入模块会从每个提交下的文件 diff 派生审阅种子。',
                external_url='https://example.invalid/workitems/wi-avatar-01',
                snapshot_hint='linked-commit-demo',
                linked_commits=[
                    WorkItemLinkedCommit(
                        commit_id='c-avatar-a',
                        commit_hash='1f4a7c82d93a4c2db6d6b3f4d7c94211c0a91320',
                        title='feat: add avatar upload entry flow',
                        author='dev.a',
                        created_at=now,
                        message='接入头像选择与上传入口，增加页面事件处理。',
                        file_diffs=[
                            WorkItemFileDiff(
                                diff_id='d-avatar-page',
                                filename='entry/src/main/ets/pages/ProfilePage.ets',
                                change_type='modified',
                                additions=14,
                                deletions=2,
                                hunks=[
                                    WorkItemDiffHunk(
                                        hunk_id='h-avatar-page-1',
                                        header='@@ -18,0 +18,7 @@',
                                        start_line=18,
                                        end_line=24,
                                        added_lines=[
                                            'async onSelectAvatar(file: File) {',
                                            '  const compressed = await this.avatarService.compressImage(file)',
                                            '  await this.avatarService.uploadAvatar(compressed)',
                                            '  showToast(\'上传成功\')',
                                            '}',
                                        ],
                                        context_lines=['private avatarService: AvatarService = new AvatarService()'],
                                    )
                                ],
                            ),
                            WorkItemFileDiff(
                                diff_id='d-avatar-service',
                                filename='entry/src/main/ets/service/AvatarService.ets',
                                change_type='added',
                                additions=18,
                                deletions=0,
                                hunks=[
                                    WorkItemDiffHunk(
                                        hunk_id='h-avatar-service-1',
                                        header='@@ -1,0 +1,9 @@',
                                        start_line=1,
                                        end_line=9,
                                        added_lines=[
                                            'async compressImage(file: File): Promise<File> {',
                                            '  const resized = await resizeImage(file, 1024)',
                                            '  return resized',
                                            '}',
                                            'async uploadAvatar(file: File) {',
                                            '  return await uploadFile(\'/api/avatar\', file)',
                                            '}',
                                        ],
                                    )
                                ],
                            ),
                        ],
                    ),
                    WorkItemLinkedCommit(
                        commit_id='c-avatar-b',
                        commit_hash='4c108e2d88bc5c691c11a3ca440404d07cb5f94f',
                        title='fix: add upload retry and failure toast',
                        author='dev.b',
                        created_at=now,
                        message='补充上传失败提示和重试包装。',
                        file_diffs=[
                            WorkItemFileDiff(
                                diff_id='d-avatar-retry',
                                filename='entry/src/main/ets/service/UploadRetry.ets',
                                change_type='added',
                                additions=16,
                                deletions=0,
                                hunks=[
                                    WorkItemDiffHunk(
                                        hunk_id='h-avatar-retry-1',
                                        header='@@ -1,0 +1,8 @@',
                                        start_line=1,
                                        end_line=8,
                                        added_lines=[
                                            'export async function retryUpload(task: () => Promise<void>) {',
                                            '  let count = 0',
                                            '  while (count < 3) {',
                                            '    try { await task(); return } catch (e) { count += 1 }',
                                            '  }',
                                            '  throw new Error(\'upload failed\')',
                                            '}',
                                        ],
                                    )
                                ],
                            )
                        ],
                    ),
                ],
            ),
            'wi-order-02': WorkItemDetail(
                connector_key=self.key,
                item_id='wi-order-02',
                requirement_id='R-ORDER-02',
                title='支付失败时订单回滚校验',
                repo_name='',
                business_tag='order',
                priority='medium',
                status='ready_for_review',
                updated_at=now,
                requirement_text='用户支付失败时，订单状态必须回滚为已取消，并记录失败原因。',
                acceptance_criteria=[
                    '支付失败后订单状态应更新为已取消。',
                    '需要记录支付失败原因。',
                    '回滚逻辑需要覆盖超时和第三方错误两类失败路径。',
                ],
                owner='产品经理B',
                notes='示例工单用于验证多 commit、多文件 diff 导入后的图证据扩展流程。',
                external_url='https://example.invalid/workitems/wi-order-02',
                snapshot_hint='linked-commit-demo',
                linked_commits=[
                    WorkItemLinkedCommit(
                        commit_id='c-order-a',
                        commit_hash='16ef5439ca3804f143b8e97dc01264c86b6dbe5d',
                        title='feat: rollback order on payment failure',
                        author='dev.c',
                        created_at=now,
                        message='补充支付失败回滚主流程。',
                        file_diffs=[
                            WorkItemFileDiff(
                                diff_id='d-order-service',
                                filename='entry/src/main/ets/service/OrderService.ets',
                                change_type='modified',
                                additions=12,
                                deletions=1,
                                hunks=[
                                    WorkItemDiffHunk(
                                        hunk_id='h-order-service-1',
                                        header='@@ -22,0 +22,7 @@',
                                        start_line=22,
                                        end_line=28,
                                        added_lines=[
                                            'const payment = await this.paymentService.pay(payload)',
                                            'if (!payment.ok) {',
                                            '  await this.cancelOrder(payload.orderId)',
                                            '  await this.logFailureReason(payload.orderId, payment.reason)',
                                            '  return { ok: false }',
                                            '}',
                                        ],
                                    )
                                ],
                            ),
                            WorkItemFileDiff(
                                diff_id='d-order-payment',
                                filename='entry/src/main/ets/service/PaymentService.ets',
                                change_type='modified',
                                additions=8,
                                deletions=0,
                                hunks=[
                                    WorkItemDiffHunk(
                                        hunk_id='h-order-payment-1',
                                        header='@@ -11,0 +11,5 @@',
                                        start_line=11,
                                        end_line=15,
                                        added_lines=[
                                            'if (response.timeout || response.thirdPartyError) {',
                                            '  return { ok: false, reason: response.errorMessage }',
                                            '}',
                                        ],
                                    )
                                ],
                            ),
                        ],
                    )
                ],
            ),
        }

    @property
    def summary(self) -> WorkItemConnectorSummary:
        return self._connector

    def list_items(self) -> List[WorkItemSummary]:
        return [
            WorkItemSummary(**item.model_dump(exclude={
                'requirement_text',
                'acceptance_criteria',
                'owner',
                'notes',
                'external_url',
                'linked_commits',
                'derived_seeds',
                'derived_seed_count',
                'snapshot_hint',
            }))
            for item in self._items.values()
        ]

    def get_item(self, item_id: str) -> WorkItemDetail:
        item = self._items.get(item_id)
        if not item:
            raise ValueError(f'work item not found: {item_id}')
        return item
