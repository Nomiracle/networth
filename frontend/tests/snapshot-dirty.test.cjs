// 中文注释：回归用例覆盖脏状态语义；用合成数据避免真实金额
const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const ts = require(path.join(root, 'node_modules/typescript'));
const vue = require(path.join(root, 'node_modules/vue'));

function loadEditState() {
  const src = fs.readFileSync(path.join(root, 'src/utils/snapshotEditState.ts'), 'utf8');
  const compiled = ts.transpileModule(src, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const m = { exports: {} };
  const ctx = { exports: m.exports, require: (n) => { throw new Error('unexpected import '+n); }, module: m };
  vm.runInNewContext(compiled, ctx);
  const mod = ctx.exports;
  if (typeof mod.isSameEdit !== 'function' || typeof mod.cloneRows !== 'function') throw new Error('editState missing');
  const a = { ccy:'CNY', amount:'1', usd:'', ccy_touched:false, note:'', credit_limit:'', credit_available:'', removed:false, added:false, orig_usd:null, orig_fx:null };
  const b = { ...a, ccy_touched:true };
  if (mod.isSameEdit(a,b) === true) throw new Error('isSameEdit fallback detected');
  return mod;
}
const editStateMod = loadEditState();

function syntheticUsdItems(n, fx, startId = 1, baseUsd = 100) {
  const items = [];
  for (let i = 0; i < n; i++) {
    const id = startId + i;
    const usd = baseUsd + i * 10;
    const cny = Math.round(usd * fx * 100) / 100;
    items.push({ account_id: id, account: `合成资产${id}`, kind: 'asset', category: '金融资产', subclass: '现金', amount_cny: cny, amount_usd: usd, fx_rate: fx, note: `原始备注${id}`, auto_filled: true, credit_limit_cny: null, credit_available_cny: null });
  }
  return items;
}
function extractScript() {
  const src = fs.readFileSync(path.join(root, 'src/views/SnapshotView.vue'), 'utf8');
  const part = src.split('<script setup lang="ts">')[1].split('</script>')[0];
  return part;
}
function createDeferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}
async function createHarness(opts) {
  const script = extractScript();
  const hooks = [];
  const storage = new Map();
  let fxCallCount = 0;
  let fetchFxImpl;
  if (opts.fetchFxImpl) {
    const userImpl = opts.fetchFxImpl;
    fetchFxImpl = async (...a) => { fxCallCount++; return userImpl(...a); };
  } else {
    fetchFxImpl = async (...a) => {
      fxCallCount++;
      if (opts.fxError) throw { status: 502, message: 'fx err' };
      return opts.fx || { rate: 7.2, source: 'ecb', source_label: 'ECB', source_date: opts.date || '2024-01-01', date: opts.date || '2024-01-01', fetched_at: new Date().toISOString(), cached: false, stale_days: 0 };
    };
  }
  const deps = {
    vue: { ...vue, onMounted: (fn) => hooks.push(fn), onBeforeUnmount: () => {} },
    'vue-router': { onBeforeRouteLeave: () => {}, useRoute: () => ({ params: { date: opts.routeDate || '' } }), useRouter: () => ({ replace: async () => {} }) },
    'element-plus': { ElMessage: { success: () => {}, warning: () => {}, error: () => {} }, ElMessageBox: { confirm: async () => {} } },
    '@/api/endpoints': {
      carryForward: async () => opts.carry,
      fetchSnapshot: async (date) => {
        if (opts.detail) { if (opts.detail.snapshot.date === date) return opts.detail; throw { status: 404 }; }
        throw { status: 404 };
      },
      fetchFxRate: fetchFxImpl,
      fetchAccounts: async () => opts.accounts || [],
      saveSnapshot: async (payload) => {
        if (opts.saveError) throw { status: 500, message: 'save fail' };
        if (opts.saveImpl) return opts.saveImpl(payload);
        return { date: payload.date };
      },
      updateSnapshotCredit: async (date, accountId, payload) => {
        if (opts.creditSaveError) throw { status: 500, message: 'credit fail' };
        if (opts.creditSaveImpl) return opts.creditSaveImpl(date, accountId, payload);
        const items = (opts.carry?.items || opts.detail?.items || []).map(it => ({ ...it }));
        const idx = items.findIndex(i => i.account_id === accountId);
        if (idx >= 0) {
          if (payload.credit_limit_cny !== undefined) items[idx].credit_limit_cny = payload.credit_limit_cny;
          if (payload.credit_available_cny !== undefined) items[idx].credit_available_cny = payload.credit_available_cny;
        }
        return { snapshot: { date, status: 'final' }, items };
      },
      createAccount: async (p) => ({ id: 9999, name: p.name, kind: p.kind, category: p.category, subclass: p.subclass }),
    },
    '@/api/client': { isApiError: e => typeof e?.status === 'number', errorText: (e) => e?.message || 'error' },
    '@/stores/settings': { useSettingsStore: () => ({ defaultFxRate: 7.0 }) },
    '@/utils/format': { todayISO: () => opts.today || '2024-01-15', fmtMoney: () => '', fmtCompact: () => '', fmtPct: () => '', fmtSigned: () => '', trendClass: () => '' },
    '@/utils/taxonomy': { DEFAULT_TAXONOMY: { asset: { category: '金融资产', subclass: '现金' } }, confirmNewTaxonomy: async () => true },
    '@/components/CategoryPicker.vue': {},
    '@/utils/snapshotEditState': editStateMod,
  };
  deps['@/api/endpoints'].fetchLocationReverse = async () => ({ text: '合成城', provider: 'nominatim', cached: false });
  const suffix = `
exports.__harness = {
  get rows(){ return rows.value; },
  get fxRate(){ return fxRate.value; },
  set fxRate(v){ fxRate.value = v; },
  get marketNote(){ return marketNote.value; },
  set marketNote(v){ marketNote.value = v; },
  get locationText(){ return locationText.value; },
  set locationText(v){ locationText.value = v; },
  get locationObservedAt(){ return locationObservedAt.value; },
  set locationObservedAt(v){ locationObservedAt.value = v; },
  get locationAccuracy(){ return locationAccuracy.value; },
  set locationAccuracy(v){ locationAccuracy.value = v; },
  get locationLat(){ return locationLat.value; },
  set locationLat(v){ locationLat.value = v; },
  get locationLon(){ return locationLon.value; },
  set locationLon(v){ locationLon.value = v; },
  get locationSource(){ return locationSource.value; },
  get formDate(){ return formDate.value; },
  set formDate(v){ formDate.value = v; },
  get mode(){ return mode.value; },
  get dirty(){ return dirty.value; },
  get unsavedRows(){ return unsavedRows.value; },
  get formDirty(){ return formDirty.value; },
  get dirtySummary(){ return dirtySummary.value; },
  get savedFingerprint(){ return savedFingerprint.value; },
  get loading(){ return loading.value; },
  get fxTouched(){ return fxTouched.value; },
  set fxTouched(v){ fxTouched.value = v; },
  get locationTouched(){ return locationTouched.value; },
  set locationTouched(v){ locationTouched.value = v; },
  load, loadFx, doSave, saveCreditRow, resetRow, resetAll, onDateChange, onRowInput, onRowUsdInput, toggleCcy, onLocationInput, onFxInput,
  restoreDraft(raw) { localDraftPayload.value = raw; restoreLocalDraft(); },
  _refs: { rows, fxRate, marketNote, locationText, locationTouched, fxTouched, savedFingerprint, mode, formDate, status, loading, fxInfo, fxLoading, locationObservedAt, locationAccuracy, locationLat, locationLon },
  _vue: require('vue'),
  _fxCallCount: () => fxCallCount,
};
`;
  const compiled = ts.transpileModule(script + suffix, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const context = {
    exports: {},
    require: (name) => {
      if (name in deps) return deps[name];
      if (name === 'vue') return deps.vue;
      throw new Error('unexpected import: ' + name);
    },
    window: {
      localStorage: { getItem: k => storage.get(k) || null, setItem: (k,v)=>storage.set(k,v), removeItem: k=>storage.delete(k) },
      setTimeout: (fn,ms)=> opts.timers ? opts.timers.push(fn) : setTimeout(fn,ms),
      clearTimeout: (id)=> clearTimeout(id),
      addEventListener: () => {},
      removeEventListener: () => {},
    },
    console,
    setTimeout, clearTimeout,
    navigator: { geolocation: { getCurrentPosition: (s,f)=> f({code:1}) } },
  };
  vm.runInNewContext(compiled, context);
  const h = context.exports.__harness;
  // 连接外部计数
  h._fxCallCount = () => fxCallCount;
  h._storage = storage;
  for (const fn of hooks) { await fn(); }
  await h._vue.nextTick();
  return { h, context, fxCallCount: () => fxCallCount, storage, hooks };
}

describe('snapshot dirty-state 回归（合成数据）', () => {

  it('新期挂载生命周期内 FX 仅调用一次且 loading 在 FX 完成前为真', async () => {
    const items = syntheticUsdItems(3, 7.0);
    const carry = { date_default: '2024-01-15', fx_rate_default: 7.0, items, location_default: null };
    let ctrl = createDeferred();
    // 单独构造可控的挂载以检查 loading 时机
    const script = extractScript();
    const hooks = [];
    const storage = new Map();
    let fxCalls = 0;
    const deps = {
      vue: { ...vue, onMounted: (fn) => hooks.push(fn), onBeforeUnmount: () => {} },
      'vue-router': { onBeforeRouteLeave: () => {}, useRoute: () => ({ params: {} }), useRouter: () => ({ replace: async () => {} }) },
      'element-plus': { ElMessage: { success: () => {}, warning: () => {}, error: () => {} }, ElMessageBox: { confirm: async () => {} } },
      '@/api/endpoints': {
        carryForward: async () => carry,
        fetchSnapshot: async () => { throw { status: 404 }; },
        fetchFxRate: async () => { fxCalls++; return ctrl.promise; },
        fetchAccounts: async () => [], saveSnapshot: async () => ({date:'2024-01-15'}), updateSnapshotCredit: async () => ({snapshot:{date:'2024-01-15'}, items:[]}), createAccount: async () => ({id:1,name:'x',kind:'asset',category:'',subclass:''})
      },
      '@/api/client': { isApiError: e => typeof e?.status === 'number', errorText: (e) => e?.message || 'error' },
      '@/stores/settings': { useSettingsStore: () => ({ defaultFxRate: 7.0 }) },
      '@/utils/format': { todayISO: () => '2024-01-15', fmtMoney: () => '', fmtCompact: () => '', fmtPct: () => '', fmtSigned: () => '', trendClass: () => '' },
      '@/utils/taxonomy': { DEFAULT_TAXONOMY: { asset: { category: '金融资产', subclass: '现金' } }, confirmNewTaxonomy: async () => true },
      '@/components/CategoryPicker.vue': {}, '@/utils/snapshotEditState': editStateMod,
    };
    deps['@/api/endpoints'].fetchLocationReverse = async () => ({ text: '合成城', provider: 'nominatim', cached: false });
    const suffix = `
exports.__harness = {
  get loading(){ return loading.value; }, get dirty(){ return dirty.value; }, get fxCallCount(){ return fxCalls; },
  _vue: require('vue')
};
`;
    const compiled = ts.transpileModule(script + suffix, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
    const ctx = {
      exports: {},
      require: (n)=>{ if(n in deps) return deps[n]; if(n==='vue') return deps.vue; throw new Error(n); },
      window: { localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)}, setTimeout, clearTimeout, addEventListener:()=>{}, removeEventListener:()=>{} },
      console, setTimeout, clearTimeout, navigator:{geolocation:{getCurrentPosition:(s,f)=>f({code:1})}}
    };
    vm.runInNewContext(compiled, ctx);
    const h2 = ctx.exports.__harness;
    const p = hooks[0]();
    await Promise.resolve();
    assert.equal(h2.loading, true, 'FX pending 时 loading 应为 true');
    assert.equal(h2.dirty, false);
    ctrl.resolve({ rate: 7.2, source:'ecb', source_label:'ECB', source_date:'2024-01-15', date:'2024-01-15', fetched_at:new Date().toISOString(), cached:false, stale_days:0 });
    await p;
    await h2._vue.nextTick();
    assert.equal(h2.loading, false);
    assert.equal(h2.dirty, false);
    assert.equal(fxCalls, 1, '挂载 FX 仅一次');
  });

  it('已存在快照不自动抓 FX', async () => {
    const items = syntheticUsdItems(2, 6.5);
    const detail = { snapshot: { date: '2024-01-10', status: 'final', default_fx_rate: 6.5, market_note: '' }, items };
    const carry = { date_default: '2024-01-15', fx_rate_default: 7.0, items: [], location_default: null };
    const { h } = await createHarness({ carry, detail, routeDate: '2024-01-10', today: '2024-01-15' });
    assert.equal(h.mode, 'edit');
    assert.equal(h.fxRate, 6.5);
    assert.equal(h._fxCallCount(), 0);
    assert.equal(h.dirty, false);
  });

  it('隔离改动计数：note/credit/removal/ccy 各自精确为 1', async () => {
    const baseItems = [...syntheticUsdItems(1, 7.0, 1), { account_id: 99, account: '合成信用卡', kind: 'liability', category: '负债', subclass: '信用卡', amount_cny: -5000, note:'', credit_limit_cny: 10000, credit_available_cny: 5000 }];
    const carry = { date_default: '2024-01-15', fx_rate_default: 7.0, items: baseItems, location_default: null };
    const fx = { rate: 7.0, source:'ecb', source_label:'ECB', source_date:'2024-01-15', date:'2024-01-15', fetched_at:new Date().toISOString(), cached:false, stale_days:0 };
    {
      const { h } = await createHarness({ carry: JSON.parse(JSON.stringify(carry)), fx, today:'2024-01-15' });
      h.rows[0].note = '新备注';
      await h._vue.nextTick();
      assert.equal(h.unsavedRows.length, 1);
      assert.equal(h.dirty, true);
      assert.equal(h.formDirty, false);
    }
    {
      const { h } = await createHarness({ carry: JSON.parse(JSON.stringify(carry)), fx, today:'2024-01-15' });
      const cr = h.rows.find(r=>r.account_id===99);
      cr.credit_limit = '20000';
      await h._vue.nextTick();
      assert.equal(h.unsavedRows.length, 1);
    }
    {
      const { h } = await createHarness({ carry: JSON.parse(JSON.stringify(carry)), fx, today:'2024-01-15' });
      h.rows[0].removed = true;
      await h._vue.nextTick();
      assert.equal(h.unsavedRows.length, 1);
    }
    {
      const { h } = await createHarness({ carry: JSON.parse(JSON.stringify(carry)), fx, today:'2024-01-15' });
      const r = h.rows[0];
      h.toggleCcy(r);
      await h._vue.nextTick();
      assert.equal(h.unsavedRows.length, 1);
      h.toggleCcy(r);
      await h._vue.nextTick();
      assert.equal(h.unsavedRows.length, 1);
      assert.equal(r.ccy_touched, true);
    }
  });

  it('null 坐标基线不变脏', async () => {
    const items = syntheticUsdItems(1, 7.0);
    const carry = { date_default: '2024-01-15', fx_rate_default: 7.0, items, location_default: null };
    const { h } = await createHarness({ carry, fx:{ rate:7.0, source:'ecb', source_label:'ECB', source_date:'2024-01-15', date:'2024-01-15', fetched_at:new Date().toISOString(), cached:false, stale_days:0 }, today:'2024-01-15' });
    assert.equal(h.locationLat, null);
    assert.equal(h.locationLon, null);
    assert.equal(h.dirty, false);
    await h._vue.nextTick();
    assert.equal(h.dirty, false);
    assert.equal(h.formDirty, false);
  });

  it('resetAll 完整恢复 device 坐标与时间戳/精度/touched', async () => {
    const detail = { snapshot: { date:'2024-01-10', status:'final', default_fx_rate:7.0, market_note:'', location:{ text:'示例城市A', source:'device', observed_at:'2024-01-10T08:00:00.000Z', lat:31.1, lon:121.5, accuracy_m:12 } }, items: syntheticUsdItems(1,7.0) };
    const carry = { date_default:'2024-01-15', fx_rate_default:7.0, items: syntheticUsdItems(1,7.0), location_default:null };
    const { h } = await createHarness({ carry, detail, routeDate:'2024-01-10', today:'2024-01-10' });
    assert.equal(h.locationText, '示例城市A');
    assert.equal(h.locationObservedAt, '2024-01-10T08:00:00.000Z');
    assert.equal(h.locationAccuracy, 12);
    h.locationText = '改后城市';
    h.locationLat = 0; h.locationLon = 0; h.locationAccuracy = 99; h.locationObservedAt = '2025-01-01T00:00:00.000Z'; h.locationTouched = true;
    h.rows[0].note = '改备注';
    await h._vue.nextTick();
    assert.equal(h.dirty, true);
    h.resetAll();
    await h._vue.nextTick();
    assert.equal(h.locationText, '示例城市A');
    assert.equal(h.locationObservedAt, '2024-01-10T08:00:00.000Z');
    assert.equal(h.locationAccuracy, 12);
    assert.equal(h.locationLat, 31.1);
    assert.equal(h.locationLon, 121.5);
    assert.equal(h.locationTouched, false);
    assert.equal(h.rows[0].note, '原始备注1');
    assert.equal(h.dirty, false);
  });

  it('窄保存仅清额度脏，同行 note/amount 保持脏', async () => {
    const items = [...syntheticUsdItems(1,7.0,1), { account_id:99, account:'合成信用卡', kind:'liability', category:'负债', subclass:'信用卡', amount_cny:-5000, note:'原始备注99', credit_limit_cny:10000, credit_available_cny:5000 }];
    const carry = { date_default:'2024-01-10', fx_rate_default:7.0, items, location_default:null };
    const detail = { snapshot: { date:'2024-01-10', status:'final', default_fx_rate:7.0, market_note:'' }, items };
    const { h } = await createHarness({ carry, detail, routeDate:'2024-01-10', today:'2024-01-10' });
    const cr = h.rows.find(r=>r.account_id===99);
    cr.credit_limit = '20000';
    cr.note = '额度行新备注';
    cr.amount = '99999';
    await h._vue.nextTick();
    assert.equal(h.unsavedRows.length, 1);
    assert.equal(h.dirty, true);
    await h.saveCreditRow(cr);
    await h._vue.nextTick();
    assert.equal(h.dirty, true);
    assert.equal(h.unsavedRows.length, 1);
    assert.equal(h.unsavedRows[0].account_id, 99);
    assert.equal(cr.note, '额度行新备注');
  });

  it('仅额度改动窄保存后完全干净', async () => {
    const items = [...syntheticUsdItems(1,7.0,1), { account_id:99, account:'合成信用卡', kind:'liability', category:'负债', subclass:'信用卡', amount_cny:-5000, note:'', credit_limit_cny:10000, credit_available_cny:5000 }];
    const carry = { date_default:'2024-01-10', fx_rate_default:7.0, items, location_default:null };
    const detail = { snapshot: { date:'2024-01-10', status:'final', default_fx_rate:7.0, market_note:'' }, items };
    const { h } = await createHarness({ carry, detail, routeDate:'2024-01-10', today:'2024-01-10' });
    const cr = h.rows.find(r=>r.account_id===99);
    cr.credit_available = '6000';
    await h._vue.nextTick();
    assert.equal(h.dirty, true);
    await h.saveCreditRow(cr);
    await h._vue.nextTick();
    assert.equal(h.dirty, false);
    assert.equal(h.unsavedRows.length, 0);
  });

  it('并发整期保存：基线仅为提交值', async () => {
    const items = syntheticUsdItems(1,7.0,1);
    const carry = { date_default:'2024-01-15', fx_rate_default:7.0, items, location_default:null };
    let saveDeferred = createDeferred();
    const saveImpl = async (payload) => { await saveDeferred.promise; return { date: payload.date }; };
    const { h } = await createHarness({ carry, fx:{ rate:7.0, source:'ecb', source_label:'ECB', source_date:'2024-01-15', date:'2024-01-15', fetched_at:new Date().toISOString(), cached:false, stale_days:0 }, today:'2024-01-15', saveImpl });
    h.rows[0].amount = '88888';
    await h._vue.nextTick();
    const savePromise = h.doSave('final');
    h.rows[0].amount = '99999';
    await h._vue.nextTick();
    saveDeferred.resolve();
    await savePromise;
    await h._vue.nextTick();
    assert.equal(h.dirty, true);
    h.resetAll();
    await h._vue.nextTick();
    assert.equal(h.rows[0].amount, '88888');
    h.rows[0].amount = '99999';
    await h._vue.nextTick();
    assert.equal(h.dirty, true);
  });

  it('信用卡并发：提交后用户新输入不被覆盖', async () => {
    const items = [...syntheticUsdItems(1,7.0,1), { account_id:99, account:'合成信用卡', kind:'liability', category:'负债', subclass:'信用卡', amount_cny:-5000, note:'', credit_limit_cny:10000, credit_available_cny:5000 }];
    const carry = { date_default:'2024-01-10', fx_rate_default:7.0, items, location_default:null };
    const detail = { snapshot: { date:'2024-01-10', status:'final', default_fx_rate:7.0, market_note:'' }, items };
    let creditDeferred = createDeferred();
    const creditSaveImpl = async (date, accountId, payload) => { await creditDeferred.promise; return { snapshot:{date, status:'final'}, items: [{ account_id: accountId, account:'合成信用卡', kind:'liability', category:'负债', subclass:'信用卡', amount_cny:-5000, credit_limit_cny: payload.credit_limit_cny, credit_available_cny: payload.credit_available_cny, credit_mismatch:false }] }; };
    const { h } = await createHarness({ carry, detail, routeDate:'2024-01-10', today:'2024-01-10', creditSaveImpl });
    const cr = h.rows.find(r=>r.account_id===99);
    cr.credit_limit = '20000';
    await h._vue.nextTick();
    const p = h.saveCreditRow(cr);
    cr.credit_limit = '30000';
    await h._vue.nextTick();
    creditDeferred.resolve();
    await p;
    await h._vue.nextTick();
    assert.equal(cr.credit_limit, '30000');
    assert.equal(h.dirty, true);
    h.resetRow(cr);
    await h._vue.nextTick();
    assert.equal(cr.credit_limit, '20000');
  });

  it('真实日期切换通过 onDateChange 触发新 load 与 FX', async () => {
    const items = syntheticUsdItems(2,7.0);
    const carry = { date_default:'2024-01-15', fx_rate_default:7.0, items, location_default:null };
    const detail = { snapshot: { date:'2024-01-15', status:'final', default_fx_rate:7.0, market_note:'' }, items };
    let fxCalls = 0;
    const fetchFxImpl = async (date) => { fxCalls++; if (date==='2024-01-20') return { rate:8.0, source:'ecb', source_label:'ECB', source_date:'2024-01-20', date:'2024-01-20', fetched_at:new Date().toISOString(), cached:false, stale_days:0 }; return { rate:7.0, source:'ecb', source_label:'ECB', source_date:date, date, fetched_at:new Date().toISOString(), cached:false, stale_days:0 }; };
    const { h } = await createHarness({ carry, detail, routeDate:'2024-01-15', today:'2024-01-15', fetchFxImpl });
    const before = fxCalls;
    await h.onDateChange('2024-01-20');
    await h._vue.nextTick();
    assert.equal(h.formDate, '2024-01-20');
    assert.ok(fxCalls > before);
    assert.equal(h.mode, 'new');
    assert.equal(h.fxRate, 8.0);
    assert.equal(h.dirty, false);
    assert.equal(h.unsavedRows.length, 0);
  });

  it('显式刷新 loadFx(refresh=true) 在 fxTouched 时仍生效', async () => {
    const items = syntheticUsdItems(1,7.0);
    const carry = { date_default:'2024-01-15', fx_rate_default:7.0, items, location_default:null };
    let fetched = false;
    const { h: h2 } = await createHarness({ carry, fetchFxImpl: async (d, refresh) => { if (refresh) { fetched = true; return { rate:9.0, source:'ecb', source_label:'ECB', source_date:d, date:d, fetched_at:new Date().toISOString(), cached:false, stale_days:0 }; } return { rate:7.0, source:'ecb', source_label:'ECB', source_date:d, date:d, fetched_at:new Date().toISOString(), cached:false, stale_days:0 }; }, today:'2024-01-15' });
    h2.onFxInput('8.5');
    await h2._vue.nextTick();
    await h2.loadFx(h2.formDate, true);
    await h2._vue.nextTick();
    assert.equal(fetched, true);
    assert.equal(h2.fxRate, 9.0);
    assert.equal(h2.dirty, true);
  });

  it('刷新期间手工改动不被覆盖', async () => {
    const items = syntheticUsdItems(1,7.0);
    const carry = { date_default:'2024-01-15', fx_rate_default:7.0, items, location_default:null };
    let ctrl = createDeferred();
    const { h: h3 } = await createHarness({ carry, fetchFxImpl: async (d, refresh) => { if (refresh) return ctrl.promise; return { rate:7.0, source:'ecb', source_label:'ECB', source_date:d, date:d, fetched_at:new Date().toISOString(), cached:false, stale_days:0 }; }, today:'2024-01-15' });
    const refreshP = h3.loadFx(h3.formDate, true);
    await Promise.resolve();
    h3.onFxInput('8.8');
    await h3._vue.nextTick();
    ctrl.resolve({ rate:9.9, source:'ecb', source_label:'ECB', source_date:'2024-01-15', date:'2024-01-15', fetched_at:new Date().toISOString(), cached:false, stale_days:0 });
    await refreshP;
    await h3._vue.nextTick();
    assert.equal(String(h3.fxRate), '8.8');
  });

  it('deferred FX 期间 loading 门控保留旧草稿', async () => {
    const items = syntheticUsdItems(1,7.0);
    const carry = { date_default:'2024-01-15', fx_rate_default:7.0, items, location_default:null };
    let ctrl = createDeferred();
    const storage = new Map([['networth_snapshot_draft', JSON.stringify({ at:'2024-01-01T00:00:00.000Z', date:'2024-01-15', rows: [{account_id:1}] })]]);
    const script = extractScript();
    const hooks=[];
    const deps = {
      vue: { ...vue, onMounted:(fn)=>hooks.push(fn), onBeforeUnmount:()=>{} },
      'vue-router': { onBeforeRouteLeave:()=>{}, useRoute:()=>({params:{}}), useRouter:()=>({replace:async()=>{}}) },
      'element-plus': { ElMessage:{success:()=>{},warning:()=>{},error:()=>{}}, ElMessageBox:{confirm:async()=>{}} },
      '@/api/endpoints': { carryForward: async()=>carry, fetchSnapshot: async()=>{throw {status:404}}, fetchFxRate: async()=>ctrl.promise, fetchAccounts: async()=>[], saveSnapshot: async()=>({date:'2024-01-15'}), updateSnapshotCredit: async()=>({snapshot:{date:'2024-01-15'},items:[]}), createAccount: async()=>({id:1,name:'x',kind:'asset',category:'',subclass:''}) },
      '@/api/client': { isApiError:e=>typeof e?.status==='number', errorText:(e)=>e?.message||'error' },
      '@/stores/settings': { useSettingsStore:()=>({defaultFxRate:7.0}) },
      '@/utils/format': { todayISO:()=>'2024-01-15', fmtMoney:()=>'', fmtCompact:()=>'', fmtPct:()=>'', fmtSigned:()=>'', trendClass:()=>'' },
      '@/utils/taxonomy': { DEFAULT_TAXONOMY:{asset:{category:'金融资产',subclass:'现金'}}, confirmNewTaxonomy:async()=>true },
      '@/components/CategoryPicker.vue': {}, '@/utils/snapshotEditState': editStateMod,
    };
    deps['@/api/endpoints'].fetchLocationReverse = async()=>({text:'合成城',provider:'nominatim',cached:false});
    const suffix = `
exports.__harness = { get loading(){return loading.value;}, get dirty(){return dirty.value;}, get rows(){return rows.value;}, _vue: require('vue') };
`;
    const compiled = ts.transpileModule(script+suffix,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
    const ctx={exports:{},require:(n)=>{if(n in deps) return deps[n]; if(n==='vue') return deps.vue; throw new Error(n);},window:{localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)}, setTimeout, clearTimeout, addEventListener:()=>{}, removeEventListener:()=>{}},console,setTimeout,clearTimeout,navigator:{geolocation:{getCurrentPosition:(s,f)=>f({code:1})}}};
    vm.runInNewContext(compiled, ctx);
    const h2=ctx.exports.__harness;
    const p=hooks[0]();
    await Promise.resolve();
    assert.equal(h2.loading, true);
    assert.equal(storage.get('networth_snapshot_draft'), JSON.stringify({ at:'2024-01-01T00:00:00.000Z', date:'2024-01-15', rows: [{account_id:1}] }));
    ctrl.resolve({ rate:7.2, source:'ecb', source_label:'ECB', source_date:'2024-01-15', date:'2024-01-15', fetched_at:new Date().toISOString(), cached:false, stale_days:0 });
    await p;
    await h2._vue.nextTick();
    assert.equal(storage.get('networth_snapshot_draft'), JSON.stringify({ at:'2024-01-01T00:00:00.000Z', date:'2024-01-15', rows: [{account_id:1}] }));
  });

  it('7 个美元行自动初始化干净；保存不清空较上期 Δ，单行还原保留美元与备注', async () => {
    const carry = {date_default:'2024-01-15', fx_rate_default:7, items:syntheticUsdItems(7,7)};
    const {h}=await createHarness({carry,fx:{rate:7.2}});
    assert.equal(h.rows.filter(r=>r.ccy==='USD').length,7);
    assert.equal(h.dirty,false);
    assert.equal(h.unsavedRows.length,0);
    const usd=h.rows[0].usd, note=h.rows[0].note;
    h.onRowUsdInput(h.rows[0],'999'); h.rows[0].note='改后备注';
    h.resetRow(h.rows[0]);
    assert.equal(h.rows[0].usd,usd); assert.equal(h.rows[0].note,note);
    h.onRowUsdInput(h.rows[0],'123');
    await h.doSave('draft');
    assert.equal(h.dirty,false); assert.equal(h.mode,'edit');
    assert.equal(h.rows[0].baseline,700);
  });

  it('FX 或写入失败不伪造已保存；表单修改不显示零项', async () => {
    const carry={date_default:'2024-01-15',fx_rate_default:7,items:syntheticUsdItems(1,7)};
    const {h}=await createHarness({carry,fxError:true,saveError:true});
    assert.equal(h.dirty,false); assert.equal(h.fxRate,7);
    h.marketNote='表单改动';
    assert.equal(h.dirty,true); assert.equal(h.dirtySummary,'表单有改动');
    const saved=h.savedFingerprint;
    await h.doSave('draft');
    assert.equal(h.savedFingerprint,saved); assert.equal(h.dirty,true);
  });

  it('旧整期保存回包不修改已切换新期基线', async () => {
    const carry={date_default:'2024-01-15',fx_rate_default:7,items:syntheticUsdItems(1,7)};
    const ctrl=createDeferred();
    const {h}=await createHarness({carry,fx:{rate:7},saveImpl:()=>ctrl.promise});
    h.rows[0].note='旧期提交';
    const saving=h.doSave('draft');
    await h.load('2024-01-20');
    const saved=h.savedFingerprint;
    ctrl.resolve({date:'2024-01-15'}); await saving;
    assert.equal(h.formDate,'2024-01-20'); assert.equal(h.savedFingerprint,saved);
    assert.equal(h.dirty,false); assert.equal(h.mode,'new');
  });

  it('还原使延迟刷新失效并清理加载标志；空行基准可还原', async () => {
    const carry={date_default:'2024-01-15',fx_rate_default:7,items:syntheticUsdItems(1,7)};
    const ctrl=createDeferred();
    const {h}=await createHarness({carry,fetchFxImpl:(date,refresh)=>refresh?ctrl.promise:Promise.resolve({rate:7})});
    const p=h.loadFx(h.formDate,true);
    h.resetAll();
    assert.equal(h._refs.fxLoading.value,false);
    ctrl.resolve({rate:9}); await p;
    assert.equal(h.fxRate,7); assert.equal(h.dirty,false);
    const {h:empty}=await createHarness({carry:{...carry,items:[]},fx:{rate:7}});
    empty.rows.push({...h.rows[0],account_id:999,added:true});
    empty.marketNote='改动'; empty.resetAll();
    assert.equal(empty.rows.length,0); assert.equal(empty.marketNote,''); assert.equal(empty.dirty,false);
  });

  it('自动草稿完整恢复表单，旧版无额度字段草稿保持未观测且可提交', async () => {
    const carry={date_default:'2024-01-15',fx_rate_default:7,items:syntheticUsdItems(1,7)};
    const timers=[];
    const {h,storage}=await createHarness({carry,fx:{rate:7},timers});
    h.marketNote='待恢复的市场备注'; h.onFxInput('8.5'); h.onLocationInput('示例城市B');
    await h._vue.nextTick();
    assert.ok(timers.length>0); timers.at(-1)();
    const raw=storage.get('networth_snapshot_draft');
    assert.equal(JSON.parse(raw).form.marketNote,'待恢复的市场备注');
    h.resetAll(); h.restoreDraft(raw);
    assert.equal(h.marketNote,'待恢复的市场备注'); assert.equal(String(h.fxRate),'8.5');
    assert.equal(h.locationText,'示例城市B'); assert.equal(h.dirty,true);
    const legacy={...h.rows[0],kind:'liability',subclass:'信用卡'};
    delete legacy.credit_limit; delete legacy.credit_available;
    delete legacy.orig_credit_limit; delete legacy.orig_credit_available;
    h.restoreDraft(JSON.stringify({date:h.formDate,rows:[legacy]}));
    assert.equal(h.rows[0].credit_limit,''); assert.equal(h.rows[0].orig_credit_limit,null);
    await h.doSave('draft'); assert.equal(h.dirty,false);
  });
});
