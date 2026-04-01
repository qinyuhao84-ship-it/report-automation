const test = require('node:test');
const assert = require('node:assert/strict');

const {
  DraftLibrary,
  InMemoryDraftStore,
  DEFAULT_UNNAMED_COMPANY,
} = require('../frontend/draft_library.js');

function createLibrary() {
  let now = 1700000000000;
  return new DraftLibrary({
    store: new InMemoryDraftStore(),
    now: () => {
      now += 1000;
      return now;
    },
  });
}

test('保存工作草稿：按企业隔离存储', async () => {
  const library = createLibrary();
  await library.saveWorkingDraft('企业A', { company_name: '企业A', product_name: '产品A' }, 101);
  const record = await library.getDraft('企业A', 'working', 0);

  assert.ok(record);
  assert.equal(record.companyName, '企业A');
  assert.equal(record.kind, 'working');
  assert.equal(record.savedTs, 101);
  assert.equal(record.snapshot.product_name, '产品A');
});

test('保存版本草稿：第N版可写入和读取', async () => {
  const library = createLibrary();
  await library.saveVersionDraft('企业A', 1, { company_name: '企业A', memo: 'v1' }, 201);

  const record = await library.getDraft('企业A', 'version', 1);
  assert.ok(record);
  assert.equal(record.kind, 'version');
  assert.equal(record.versionNo, 1);
  assert.equal(record.snapshot.memo, 'v1');
});

test('同公司同版本覆盖：后写入覆盖前写入', async () => {
  const library = createLibrary();
  await library.saveVersionDraft('企业A', 1, { company_name: '企业A', memo: 'v1-old' }, 300);
  await library.saveVersionDraft('企业A', 1, { company_name: '企业A', memo: 'v1-new' }, 301);

  const record = await library.getDraft('企业A', 'version', 1);
  assert.ok(record);
  assert.equal(record.snapshot.memo, 'v1-new');
  assert.equal(record.savedTs, 301);
});

test('跨公司隔离：A公司和B公司互不影响', async () => {
  const library = createLibrary();
  await library.saveWorkingDraft('企业A', { company_name: '企业A', memo: 'A-working' }, 401);
  await library.saveWorkingDraft('企业B', { company_name: '企业B', memo: 'B-working' }, 402);
  await library.saveVersionDraft('企业A', 2, { company_name: '企业A', memo: 'A-v2' }, 403);

  const companyA = await library.listCompanyDrafts('企业A');
  const companyB = await library.listCompanyDrafts('企业B');

  assert.equal(companyA.length, 2);
  assert.equal(companyB.length, 1);
  assert.equal(companyB[0].snapshot.memo, 'B-working');
});

test('删除单个版本：仅删除目标版本不影响工作草稿', async () => {
  const library = createLibrary();
  await library.saveWorkingDraft('企业A', { company_name: '企业A', memo: 'working' }, 501);
  await library.saveVersionDraft('企业A', 1, { company_name: '企业A', memo: 'v1' }, 502);
  await library.saveVersionDraft('企业A', 2, { company_name: '企业A', memo: 'v2' }, 503);

  await library.deleteVersionDraft('企业A', 1);

  const version1 = await library.getDraft('企业A', 'version', 1);
  const version2 = await library.getDraft('企业A', 'version', 2);
  const working = await library.getDraft('企业A', 'working', 0);

  assert.equal(version1, null);
  assert.ok(version2);
  assert.ok(working);
});

test('删除整家公司：该公司全部工作草稿与版本都删除', async () => {
  const library = createLibrary();
  await library.saveWorkingDraft('企业A', { company_name: '企业A', memo: 'A-working' }, 601);
  await library.saveVersionDraft('企业A', 1, { company_name: '企业A', memo: 'A-v1' }, 602);
  await library.saveWorkingDraft('企业B', { company_name: '企业B', memo: 'B-working' }, 603);

  const deleted = await library.deleteCompany('企业A');
  const companyA = await library.listCompanyDrafts('企业A');
  const companyB = await library.listCompanyDrafts('企业B');

  assert.equal(deleted, 2);
  assert.equal(companyA.length, 0);
  assert.equal(companyB.length, 1);
});

test('迁移旧单槽草稿：迁移到对应企业工作草稿', async () => {
  const library = createLibrary();
  const legacy = {
    saved_ts: 701,
    company_name: '旧企业',
    product_name: '旧产品',
    template_type: 'self',
  };

  const migrated = await library.migrateLegacySnapshot(legacy);
  const working = await library.getDraft('旧企业', 'working', 0);

  assert.equal(migrated.migrated, true);
  assert.equal(migrated.companyName, '旧企业');
  assert.ok(working);
  assert.equal(working.savedTs, 701);
  assert.equal(working.snapshot.product_name, '旧产品');
});

test('保留版本前置校验：企业名为空时禁止保存版本', async () => {
  const library = createLibrary();
  await assert.rejects(
    () => library.saveVersionDraft('', 1, { company_name: '' }, 801),
    /请先填写企业名称，再保留版本/
  );

  const missingCompanyDraft = await library.getDraft(DEFAULT_UNNAMED_COMPANY, 'version', 1);
  assert.equal(missingCompanyDraft, null);
});
