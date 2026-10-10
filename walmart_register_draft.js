// Passed to the Finance Assistant handoff; no token or credentials leave this node.
const source = $('拉取沃尔玛利润数据').first().json;
const written = $('合并数据+写入表格').first().json;
const rates = $('获取Token+汇率').first().json;
const job = $('月度生成闸').first().json;
const numberFields = ['salesNum','refundNum','salesAmount','buyerFreightAmount','promotionDiscountAmount',
 'platformOtherIncomeAmount','subsidyAmount','refundAmount','productCommission','platformLogisticsAmount',
 'platformStorageAmount','advertisementAmount','adjustmentCostAmount','platformOtherAmount','platformFineAmount',
 'promotionExtendAmount','taxAmount','marketTaxAmount','salesPurchaseAmount','returnedPurchaseAmount','otherAmount',
 'salesTransportationAmount','returnedTransportationAmount','tailAmount','grossProfit'];
const snapshot = (source.mskuList || []).map(row => Object.fromEntries(
 ['storeName','msku','currencyCode', ...numberFields].map(key => [key, row[key] ?? null])))
 .sort((a,b) => (a.storeName+'|'+a.msku).localeCompare(b.storeName+'|'+b.msku));
const errors = [];
if (!snapshot.length) errors.push('本月结算毛利接口为空，需确认无结算或数据未就绪。');
if ((written.writeResults || []).some(x => x.code !== 0) || written.totalRows !== snapshot.length)
 errors.push('写表回执与取数行数不一致。');
if (rates.rateFallback) errors.push('本月汇率不可用，已用兜底值生成草稿；请补本月汇率后重跑。');
if (source.coverage?.status === 'coverage_source_unavailable') errors.push('订单覆盖接口异常，无法核对两店覆盖。');
for (const row of snapshot) {
 if (numberFields.some(key => row[key] !== null && !Number.isFinite(Number(row[key]))))
  errors.push('非数字金额：'+row.storeName+'/'+row.msku);
 if (!(rates.rateMap[row.currencyCode || 'USD'] > 0)) errors.push('缺少币种汇率：'+row.currencyCode);
 const n = key => Number(row[key] || 0);
 const grouped = [
  ['salesAmount','buyerFreightAmount','promotionDiscountAmount','platformOtherIncomeAmount','subsidyAmount'],
  ['refundAmount'],['productCommission'],['platformLogisticsAmount'],['platformStorageAmount'],['advertisementAmount'],
  ['adjustmentCostAmount','platformOtherAmount','platformFineAmount'],['promotionExtendAmount'],['taxAmount','marketTaxAmount'],
  ['salesPurchaseAmount','returnedPurchaseAmount','otherAmount'],
  ['salesTransportationAmount','returnedTransportationAmount','tailAmount']];
 const calculated = grouped.reduce((s, keys) => s + Math.round(keys.reduce((v,key)=>v+n(key),0)*100)/100,0);
 if (row.grossProfit === null || Math.abs(calculated - n('grossProfit')) > .06)
  errors.push('ERP毛利与费用合计不符：'+row.storeName+'/'+row.msku);
}
const hash = require('crypto').createHash('sha256').update(JSON.stringify({snapshot,rateMap:rates.rateMap})).digest('hex');
return [{json:{mode:'register',run_id:job.run_id,job_id:job.job_id,period:job.period,
 sheet_url:written.sheetUrl,source_hash:hash,source_snapshot:snapshot,coverage:source.coverage,
 validation:{version:'walmart_monthly_handoff_20261010',errors:[...new Set(errors)],rows:snapshot.length,
  retry_generation:source.coverage?.status==='coverage_source_unavailable',
  rateMap:rates.rateMap,rateFallback:!!rates.rateFallback,
  checked:['ERP字段计算及舍入','逐行写入回读','结算与订单覆盖','当月汇率','采购成本缺口由财务助手复核'],
  method_baseline:'2026-09官方结算与ERP逐项核验+财务终审；绩效广告表不等于结算扣款'}}}];
