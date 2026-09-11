# 美客多最终核销确认接入：测试阶段

## 范围

用户授权财务助手接入美客多最终核销。当前交付是同App发送、严格鉴权回调、持久确认记录、原卡更新与回读的Frankie-only测试。不是最终核销上线，不迁工资、付款或其他财务流程。

发送入口 `/finance-assistant/ml-final/test/sample`：沿用内部鉴权；在财务助手命名空间解析Frankie，只向其union_id发送。固定测试批次有持久投递预留；遇投递结果不明停止自动重发，需核对飞书收据后恢复。

回调沿用 `/finance-assistant/r5/callback` 严格Verification Token校验，仅新增 `ml_final_test_confirm` / `ml_final_test_v1` 分支。业务记录在ML服务 `/data/ml_sync.db` 独立测试表中保存，真实月结状态与报表不动。测试只走运营确认一步，不能代表完整双人流程已验收。

`ML_FINAL_TEST_FEEDBACK_WORKER=true` 时，后台每30秒重试未完成原卡反馈；PATCH成功且GET回读含结果文本后才完成待办。后台失败只记录安全提示，不输出令牌。重复PATCH不新增消息。

## 检查

`run_tests_offline.py` 阻断requests/httpx外部访问。30项测试通过；新增测试覆盖卡片结构、投递不明不重发、命名空间、回调鉴权、消息编号保留、PATCH/回读失败不清待办。此数字不包含真实用户点击。

## 回滚

关闭 `ML_FINAL_TEST_FEEDBACK_WORKER`，财务服务恢复c48f8c1；ML服务恢复d64a2dd。保留新增SQLite测试表作为证据，不删除已确认经营暂结。不得把回滚写成删除真实月结或财务数据。

## 仍未实现

官方完整账单云端收集、真实A/B核对与费用分摊、47列核销候选报表、正式两人卡片、9月18日提醒与正式汇总升级。现有测试入口不能代替这些步骤。
