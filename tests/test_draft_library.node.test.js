const test = require('node:test');
const assert = require('node:assert/strict');

const {
  DraftLibrary,
  InMemoryDraftStore,
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

test('保存版本：按企业与版本号读取', async () => {
  const library = createLibrary();
  await library.saveVersionDraft('企业A', 1, { company_name: '企业A', memo: 'v1' }, 101);

  const record = await library.getDraft('企业A', 'version', 1);
  assert.ok(record);
  assert.equal(record.companyName, '企业A');
  assert.equal(record.kind, 'version');
  assert.equal(record.versionNo, 1);
  assert.equal(record.savedTs, 101);
  assert.equal(record.snapshot.memo, 'v1');
});

test('同公司同版本覆盖：后写入覆盖前写入', async () => {
  const library = createLibrary();
  await library.saveVersionDraft('企业A', 1, { company_name: '企业A', memo: 'old' }, 201);
  await library.saveVersionDraft('企业A', 1, { company_name: '企业A', memo: 'new' }, 202);

  const record = await library.getDraft('企业A', 'version', 1);
  assert.ok(record);
  assert.equal(record.snapshot.memo, 'new');
  assert.equal(record.savedTs, 202);
});

test('跨公司隔离：A/B 公司互不影响', async () => {
  const library = createLibrary();
  await library.saveVersionDraft('企业A', 1, { company_name: '企业A', memo: 'A-v1' }, 301);
  await library.saveVersionDraft('企业A', 2, { company_name: '企业A', memo: 'A-v2' }, 302);
  await library.saveVersionDraft('企业B', 1, { company_name: '企业B', memo: 'B-v1' }, 303);

  const companyA = await library.listCompanyDrafts('企业A');
  const companyB = await library.listCompanyDrafts('企业B');

  assert.equal(companyA.length, 2);
  assert.equal(companyA[0].versionNo, 1);
  assert.equal(companyA[1].versionNo, 2);
  assert.equal(companyB.length, 1);
  assert.equal(companyB[0].snapshot.memo, 'B-v1');
});

test('删除单个版本：仅删除目标版本', async () => {
  const library = createLibrary();
  await library.saveVersionDraft('企业A', 1, { company_name: '企业A', memo: 'v1' }, 401);
  await library.saveVersionDraft('企业A', 2, { company_name: '企业A', memo: 'v2' }, 402);

  await library.deleteVersionDraft('企业A', 1);

  const version1 = await library.getDraft('企业A', 'version', 1);
  const version2 = await library.getDraft('企业A', 'version', 2);
  assert.equal(version1, null);
  assert.ok(version2);
});

test('删除整家公司：该公司全部版本删除', async () => {
  const library = createLibrary();
  await library.saveVersionDraft('企业A', 1, { company_name: '企业A', memo: 'A-v1' }, 501);
  await library.saveVersionDraft('企业A', 2, { company_name: '企业A', memo: 'A-v2' }, 502);
  await library.saveVersionDraft('企业B', 1, { company_name: '企业B', memo: 'B-v1' }, 503);

  const deleted = await library.deleteCompany('企业A');
  const companyA = await library.listCompanyDrafts('企业A');
  const companyB = await library.listCompanyDrafts('企业B');

  assert.equal(deleted, 2);
  assert.equal(companyA.length, 0);
  assert.equal(companyB.length, 1);
});

test('下一版号：按企业内最大版本号 + 1', async () => {
  const library = createLibrary();
  assert.equal(await library.getNextVersionNo('企业A'), 1);

  await library.saveVersionDraft('企业A', 1, { company_name: '企业A' }, 601);
  await library.saveVersionDraft('企业A', 3, { company_name: '企业A' }, 602);

  assert.equal(await library.getNextVersionNo('企业A'), 4);
  assert.equal(await library.getNextVersionNo('企业B'), 1);
});

test('版本校验：版本号必须 >= 1', async () => {
  const library = createLibrary();

  await assert.rejects(
    () => library.saveVersionDraft('企业A', 0, { company_name: '企业A' }, 701),
    /版本号必须是大于等于 1 的整数/
  );

  await assert.rejects(
    () => library.getDraft('企业A', 'version', 0),
    /版本号必须是大于等于 1 的整数/
  );
});

test('企业名校验：企业名称不能为空', async () => {
  const library = createLibrary();

  await assert.rejects(
    () => library.saveVersionDraft('', 1, { company_name: '' }, 801),
    /企业名称不能为空/
  );
});

test('读取校验：只允许读取版本草稿', async () => {
  const library = createLibrary();

  await assert.rejects(
    () => library.getDraft('企业A', 'working', 1),
    /仅支持读取版本草稿/
  );
});
