# 最终设计

实现独立 `mtf_direction_15m5m_v1` 研究入口，先验证 immutable catalog manifest、真实 OHLCV、时间戳/index、session 连续性和 sealed cutoff；缺任一项返回 `unavailable`。只有真实 reader 接入后才运行线段/分型和方向标签；现有 price/volume 重建分钟接口不得作为替代。结果只含方向研究证据和删失原因，不进入 short_pool/Agent，不含执行或止损语义。

编码交付包含严格输入模型、fail-closed API/服务门禁及可验证夹具；真实 reader 和 OOS 研究是后续能力依赖，不在没有证据时宣称完成。
