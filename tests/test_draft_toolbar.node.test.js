const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '../frontend/index.html'), 'utf8');

test('顶部版本中心操作入口存在', () => {
  assert.match(html, /id="createCompanyBtnTop"[^>]*onclick="onCreateCompanyClick\(\)"/);
  assert.match(html, /id="createVersionBtnTop"[^>]*onclick="onCreateVersionClick\(\)"/);
  assert.match(html, /id="saveDraftBtnTop"[^>]*onclick="onSaveDraftClick\(\)"/);
  assert.match(html, /id="deleteVersionBtnTop"[^>]*onclick="onDeleteVersionClick\(\)"/);
  assert.match(html, /id="deleteCompanyBtnTop"[^>]*onclick="onDeleteCompanyClick\(\)"/);
});

test('旧入口已移除：恢复、清空、手选目标版本号', () => {
  assert.doesNotMatch(html, /恢复所选/);
  assert.doesNotMatch(html, /清空草稿/);
  assert.doesNotMatch(html, /draftTargetVersionNo/);
  assert.doesNotMatch(html, /saveDraftVersion\(/);
  assert.doesNotMatch(html, /clearDraft\(/);
});

test('版本下拉切换即加载当前版本', () => {
  assert.match(html, /id="draftVersionSelect" onchange="onDraftVersionChange\(\)"/);
  assert.match(html, /async function onDraftVersionChange\(\) \{/);
  assert.match(html, /await loadDraft\(true\);/);
});

test('按钮交互增强：状态机、防连点、快捷键', () => {
  assert.match(html, /function updateTopActionState\(\) \{/);
  assert.match(html, /async function runTopAction\(buttonId, busyText, action\) \{/);
  assert.match(html, /function bindDraftHotkeys\(\) \{/);
  assert.match(html, /if \(\(event\.ctrlKey \|\| event\.metaKey\).*key === "s"\)/);
});

test('第一章按企业缓存：跨版本复用，不写入版本快照', () => {
  assert.match(html, /const OTHER_CHAPTER1_CACHE_KEY = "report_other_chapter1_by_company_v1"/);
  assert.match(html, /function getOtherChapter1Cache\(companyName\) \{/);
  assert.match(html, /function setOtherChapter1Cache\(companyName, sections, productName = ""\) \{/);
  assert.match(html, /if \(!force\) \{[\s\S]*getOtherChapter1Cache\(companyName\)/);
  assert.match(html, /setOtherChapter1Cache\(companyName, otherProofChapter1Sections, product\);/);
  assert.doesNotMatch(html, /other_chapter1_sections/);
});

test('第一章重新生成只能显式触发', () => {
  assert.match(html, /onclick="regenerateOtherChapter1\(\)"/);
  assert.match(html, /async function regenerateOtherChapter1\(\) \{/);
  assert.match(html, /ensureOtherChapter1\(true\)/);
  assert.match(html, /ensureOtherChapter1\(false\)/);
});

test('图表标题前缀自动生成，用户只填写后半句', () => {
  assert.match(html, /class="chart-prefix">图表1：<\/span>/);
  assert.match(html, /class="s-chart-suffix"/);
  assert.match(html, /class="s-c23"/);
  assert.match(html, /class="s-c24"/);
  assert.match(html, /class="s-c25"/);
  assert.match(html, /function extractChartTitleSuffix\(rawTitle\) \{/);
  assert.match(html, /function validateSourceChartData\(sources, contextLabel = "数据来源"\) \{/);
  assert.match(html, /chart_title: `图表\$\{idx \+ 1\}：\$\{suffix\}`/);
  assert.match(html, /if \(block\.name \|\| block\.url \|\| suffix \|\| block\.analysis \|\| block\.chart_2023 \|\| block\.chart_2024 \|\| block\.chart_2025\) list\.push\(block\);/);
});

test('经营数据市场规模自动映射自最底层来源', () => {
  assert.match(html, /<input id="total_mkt_23" readonly \/>/);
  assert.match(html, /<input id="total_mkt_24" readonly \/>/);
  assert.match(html, /<input id="total_mkt_25" readonly \/>/);
  assert.match(html, /function syncBusinessMarketScaleFromSources\(\) \{/);
  assert.match(html, /const bottom = sources\.length \? sources\[sources\.length - 1\] : null;/);
  assert.match(html, /syncBusinessMarketScaleFromSources\(\);\s*const company = document\.getElementById\("company_name"\)\.value\.trim\(\);/);
});

test('竞争对手输入不自动跳格，也不自动重排行', () => {
  assert.match(
    html,
    /function competitorInputChanged\(input, year, mode\) \{[\s\S]*refreshCompetitorBoard\(\{ sortRows: false \}\);/
  );
  assert.doesNotMatch(
    html,
    /function competitorInputChanged\(input, year, mode\) \{[\s\S]*\.focus\(/,
  );
});

test('他证支持第一章失败后跳过继续生成', () => {
  assert.match(html, /id="skipChapter1OnFailure"/);
  assert.match(html, /id="stopChapter1Btn"/);
  assert.match(html, /if \(resp\.status === 504\) \{/);
  assert.match(html, /可勾选“第一章失败后跳过继续生成”/);
  assert.match(html, /function abortOtherChapter1Generation\(\) \{/);
  assert.match(html, /signal: otherChapter1AbortController\.signal/);
  assert.match(html, /const skipChapter1OnFailure = document\.getElementById\("skipChapter1OnFailure"\)\?\.checked === true;/);
  assert.match(html, /if \(!skipChapter1OnFailure\) return;/);
  assert.match(html, /第一章：生成失败，已跳过并写入占位内容/);
});
