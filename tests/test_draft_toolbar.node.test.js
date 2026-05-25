const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '../frontend/index.html'), 'utf8');
const appJs = fs.readFileSync(path.join(__dirname, '../frontend/app.js'), 'utf8');
const chapter1ConfigJs = fs.readFileSync(path.join(__dirname, '../frontend/modules/other_chapter1_config.js'), 'utf8');
const source = [html, appJs, chapter1ConfigJs].join('\n');

test('顶部版本中心操作入口存在', () => {
  assert.match(source, /id="createCompanyBtnTop"[^>]*onclick="onCreateCompanyClick\(\)"/);
  assert.match(source, /id="createVersionBtnTop"[^>]*onclick="onCreateVersionClick\(\)"/);
  assert.match(source, /id="saveDraftBtnTop"[^>]*onclick="onSaveDraftClick\(\)"/);
  assert.match(source, /id="deleteVersionBtnTop"[^>]*onclick="onDeleteVersionClick\(\)"/);
  assert.match(source, /id="deleteCompanyBtnTop"[^>]*onclick="onDeleteCompanyClick\(\)"/);
});

test('旧入口已移除：恢复、清空、手选目标版本号', () => {
  assert.doesNotMatch(source, /恢复所选/);
  assert.doesNotMatch(source, /清空草稿/);
  assert.doesNotMatch(source, /draftTargetVersionNo/);
  assert.doesNotMatch(source, /saveDraftVersion\(/);
  assert.doesNotMatch(source, /clearDraft\(/);
});

test('版本下拉切换即加载当前版本', () => {
  assert.match(source, /id="draftVersionSelect" onchange="onDraftVersionChange\(\)"/);
  assert.match(source, /async function onDraftVersionChange\(\) \{/);
  assert.match(source, /await loadDraft\(true\);/);
});

test('按钮交互增强：状态机、防连点、快捷键', () => {
  assert.match(source, /function updateTopActionState\(\) \{/);
  assert.match(source, /async function runTopAction\(buttonId, busyText, action\) \{/);
  assert.match(source, /function bindDraftHotkeys\(\) \{/);
  assert.match(source, /if \(\(event\.ctrlKey \|\| event\.metaKey\).*key === "s"\)/);
});

test('第一章按企业缓存：跨版本复用，不写入版本快照', () => {
  assert.match(source, /cacheKey: "report_other_chapter1_by_company_v1"/);
  assert.match(source, /const OTHER_CHAPTER1_CACHE_KEY = ReportAutomationChapter1Config\.cacheKey/);
  assert.match(source, /function chapter1SectionsContainPlaceholder\(sections\) \{/);
  assert.match(source, /slot_count:\s*6/);
  assert.match(source, /if \(paragraphs\.length < spec\.slot_count\) return true;/);
  assert.match(source, /text\.startsWith\("该部分生成失败"\)/);
  assert.match(source, /function isReusableOtherChapter1CacheEntry\(entry, productName = ""\) \{/);
  assert.match(source, /function getOtherChapter1Cache\(companyName, productName = ""\) \{/);
  assert.match(source, /if \(!isReusableOtherChapter1CacheEntry\(entry, productName\)\) \{/);
  assert.match(source, /delete otherProofChapter1CacheByCompany\[key\];/);
  assert.match(source, /function setOtherChapter1Cache\(companyName, sections, productName = ""\) \{/);
  assert.match(source, /if \(!force\) \{[\s\S]*getOtherChapter1Cache\(companyName,\s*product\)/);
  assert.match(source, /const hasPlaceholderSection = chapter1SectionsContainPlaceholder\(otherProofChapter1Sections\);/);
  assert.match(source, /if \(hasPlaceholderSection\) \{[\s\S]*clearOtherChapter1Cache\(companyName\);/);
  assert.match(source, /if \(hasPlaceholderSection\) \{[\s\S]*失败位置已占位[\s\S]*return true;/);
  assert.match(source, /setOtherChapter1Cache\(companyName, otherProofChapter1Sections, product\);/);
  assert.doesNotMatch(source, /other_chapter1_sections/);
});

test('第一章重新生成只能显式触发', () => {
  assert.match(source, /onclick="regenerateOtherChapter1\(\)"/);
  assert.match(source, /async function regenerateOtherChapter1\(\) \{/);
  assert.match(source, /ensureOtherChapter1\(true,\s*false\)/);
  assert.match(source, /ensureOtherChapter1\(false,\s*false\)/);
});

test('图表标题前缀自动生成，用户只填写后半句', () => {
  assert.match(source, /class="chart-prefix">图表1：<\/span>/);
  assert.match(source, /class="s-chart-suffix"/);
  assert.match(source, /class="s-c23"/);
  assert.match(source, /class="s-c24"/);
  assert.match(source, /class="s-c25"/);
  assert.match(source, /function addSourceMultiInput\(button, type, value = ""\) \{/);
  assert.match(source, /function collectSourceMultiValues\(card, type\) \{/);
  assert.match(source, /function extractChartTitleSuffix\(rawTitle\) \{/);
  assert.match(source, /function validateSourceChartData\(sources, contextLabel = "数据来源"\) \{/);
  assert.match(source, /chart_title: `图表\$\{idx \+ 1\}：\$\{suffix\}`/);
  assert.match(source, /names,\s*url: urls\[0\] \|\| "",\s*urls,/);
  assert.match(source, /if \(block\.names\.length \|\| block\.urls\.length \|\| suffix \|\| block\.analysis \|\| block\.chart_2023 \|\| block\.chart_2024 \|\| block\.chart_2025\) list\.push\(block\);/);
});

test('经营数据市场规模支持手填且来源优先', () => {
  assert.match(source, /<input id="total_mkt_23" oninput="onMarketInputChange\('23'\)" \/>/);
  assert.match(source, /<input id="total_mkt_24" oninput="onMarketInputChange\('24'\)" \/>/);
  assert.match(source, /<input id="total_mkt_25" oninput="onMarketInputChange\('25'\)" \/>/);
  assert.match(source, /function onMarketInputChange\(year\) \{/);
  assert.match(source, /function syncBusinessMarketScaleFromSources\(\) \{/);
  assert.match(source, /const bottom = sources\.length \? sources\[sources\.length - 1\] : null;/);
  assert.match(source, /if \(nextValue && input\.value !== nextValue\) \{/);
  assert.match(source, /function resolveEffectiveMarketScale\(year, sources\) \{/);
  assert.match(source, /function convertMarketScaleYiToWan\(rawValue\) \{/);
  assert.match(source, /const wanValue = yiValue \* 10000;/);
  assert.match(source, /syncBusinessMarketScaleFromSources\(\);\s*const company = document\.getElementById\("company_name"\)\.value\.trim\(\);/);
});

test('竞争对手输入不自动跳格，也不自动重排行', () => {
  assert.match(
    source,
    /function competitorInputChanged\(input, year, mode\) \{[\s\S]*refreshCompetitorBoard\(\{ sortRows: false \}\);/
  );
  assert.doesNotMatch(
    source,
    /function competitorInputChanged\(input, year, mode\) \{[\s\S]*\.focus\(/,
  );
});

test('他证第一章部分失败时继续导出并显示回放路径', () => {
  assert.doesNotMatch(source, /id="skipChapter1OnFailure"/);
  assert.match(source, /id="stopChapter1Btn"/);
  assert.match(source, /function abortOtherChapter1Generation\(\) \{/);
  assert.match(source, /signal: otherChapter1AbortController\.signal/);
  assert.match(source, /allow_partial:\s*false/);
  assert.match(source, /formatApiErrorDetail\(err, chapter1RetryTip\)/);
  assert.match(source, /调试回放文件/);
  assert.match(source, /data\.chapter1_replay_file_path = otherProofChapter1ReplayFilePath;/);
  assert.match(source, /data\.skip_chapter1 = false;/);
  assert.match(source, /X-Chapter1-Replay-File-Path/);
  assert.doesNotMatch(source, /skipChapter1ForExport/);
  assert.doesNotMatch(source, /最终 Word 不写入第一章正文/);
});

test('导出文件名：自证按公司名，他证按产品名', () => {
  assert.match(source, /function buildOutputFileName\(data\) \{/);
  assert.match(source, /const namePart = data\.template_type === "other"/);
  assert.match(source, /\? sanitizeFileNamePart\(data\.product_name \|\| "产品名称"\)/);
  assert.match(source, /: sanitizeFileNamePart\(data\.company_name \|\| "企业名称"\)/);
  assert.match(source, /return `\$\{mm\}\$\{dd\}-\$\{namePart\}-\$\{versionNo\}版\.docx`;/);
});
