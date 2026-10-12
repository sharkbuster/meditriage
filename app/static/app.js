/* ============================================================
   门诊预约 · 患者端交互
   原则：
     - 界面上不出现任何技术词（patient_id / confidence / schedule_id 都不露出来）
     - 每个请求都处理失败情况，出错要说人话，不能白屏
     - 不用任何框架，file:// 打开也能看结构，起服务就能用
   ============================================================ */

const $ = (sel) => document.querySelector(sel);

const WEEK = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

const state = {
  departments: [],
  doctors: [],
  schedules: [],
  deptById: new Map(),
  doctorById: new Map(),
  triage: null,          // 最近一次分诊结果
  pendingSlot: null,     // 正在确认的号
  pendingDeptId: null,   // 这个号属于哪个科室（用于展示）
};

/* ---------------- 通用 ---------------- */

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  let body = null;
  try { body = await res.json(); } catch (e) { /* 可能是空响应 */ }
  if (!res.ok) {
    // FastAPI 的错误在 detail 里；校验错误是数组，取第一条的 message
    let msg = '服务暂时不可用，请稍后再试';
    if (body && body.detail) {
      msg = typeof body.detail === 'string'
        ? body.detail
        : (body.detail[0] && body.detail[0].msg) || msg;
    }
    throw new Error(msg);
  }
  return body;
}

function esc(text) {
  return String(text == null ? '' : text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/** 2026-10-12 -> 10月12日 周一 */
function prettyDate(iso) {
  if (!iso) return '';
  const d = new Date(iso + 'T00:00:00');
  return `${d.getMonth() + 1}月${d.getDate()}日 ${WEEK[d.getDay()]}`;
}

function periodRange(slot) {
  const start = (slot.start_time || '').slice(0, 5);
  const end = (slot.end_time || '').slice(0, 5);
  return `${slot.period} ${start}–${end}`;
}

/* ---------------- 顶部导航 ---------------- */

function switchView(name) {
  ['triage', 'book', 'pickup', 'mine'].forEach((v) => {
    const el = $('#view-' + v);
    if (el) el.hidden = (v !== name);
  });
  document.querySelectorAll('.tab').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.view === name);
  });
  window.scrollTo({ top: 0, behavior: 'instant' in window ? 'instant' : 'auto' });

  if (name === 'book') loadSchedules();
  if (name === 'pickup') loadMedicines();
}

document.querySelectorAll('.tab').forEach((btn) => {
  btn.addEventListener('click', () => switchView(btn.dataset.view));
});

/* ---------------- 基础数据 ---------------- */

async function loadBase() {
  const [depts, docs] = await Promise.all([
    api('/api/departments'),
    api('/api/doctors'),
  ]);
  state.departments = depts;
  state.doctors = docs;
  state.deptById = new Map(depts.map((d) => [d.id, d]));
  state.doctorById = new Map(docs.map((d) => [d.id, d]));
}

/* ---------------- 一、分诊 ---------------- */

$('#symptom').addEventListener('input', (e) => {
  $('#symptom-count').textContent = e.target.value.length;
});

$('#btn-triage').addEventListener('click', runTriage);

async function runTriage() {
  const symptom = $('#symptom').value.trim();
  const box = $('#triage-result');
  const btn = $('#btn-triage');

  if (symptom.length < 2) {
    box.hidden = false;
    box.innerHTML = '<p class="error">请再多写几个字，比如「咳嗽三天还发烧」。</p>';
    return;
  }

  btn.disabled = true;
  btn.textContent = '正在看…';
  try {
    const r = await api('/api/triage', {
      method: 'POST',
      body: JSON.stringify({ symptom, is_child: $('#is-child').checked }),
    });
    state.triage = r;
    box.hidden = false;
    if (r.need_human) renderHuman(r, box);
    else renderPicks(r, box);
  } catch (err) {
    box.hidden = false;
    box.innerHTML = `<p class="error">${esc(err.message)}</p>`;
  } finally {
    btn.disabled = false;
    btn.textContent = '帮我找科室';
  }
}

function renderHuman(r, box) {
  // 覆盖率不够，别硬推荐 —— 让患者去导诊台，这是安全兜底
  box.innerHTML = `
    <div class="route-human">
      <div class="name">建议先到导诊台问问护士</div>
      <p>${esc(r.advice || '您描述的症状不太好判断，护士帮您看一下更快。')}</p>
    </div>
    <div class="field" style="margin-top:20px">
      <label>也可以直接选科室挂号</label>
      <button class="ghost" style="width:100%" id="go-all">看看有哪些科室</button>
    </div>`;
  $('#go-all').addEventListener('click', () => switchView('book'));
}

function renderPicks(r, box) {
  const picks = r.candidates || [];
  if (!picks.length) {
    box.innerHTML = '<p class="error">暂时没匹配到科室，建议到导诊台咨询。</p>';
    return;
  }
  const top = picks[0];
  const rest = picks.slice(1);

  box.innerHTML = `
    <div class="pick">
      <div class="name">${esc(top.department_name)}</div>
      <div class="why">${esc(top.reason || '')}</div>
      <button class="primary go" data-dept="${top.department_id}">看这个科的号</button>
    </div>
    ${rest.length ? `
      <div class="alt-title">也可以考虑</div>
      ${rest.map((c) => `
        <div class="pick alt">
          <div class="name">${esc(c.department_name)}</div>
          <button class="ghost go" data-dept="${c.department_id}">看这个科的号</button>
        </div>`).join('')}
      <details class="more">
        <summary>还有别的选择</summary>
        <ul>${state.departments.map((d) => `<li>${esc(d.name)}</li>`).join('')}</ul>
      </details>` : ''}`;

  box.querySelectorAll('[data-dept]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const deptId = Number(btn.dataset.dept);
      if (!state.schedules.length) {
        // 号源还没拉过：先进预约页触发加载，等它渲染完再进科室
        switchView('book');
        loadSchedules().then(() => openDepartment(deptId));
      } else {
        switchView('book');
        openDepartment(deptId);
      }
    });
  });
}

/* ---------------- 二、预约（两步：先选科室，再看号）---------------- */

async function loadSchedules() {
  const grid = $('#dept-grid');
  grid.innerHTML = '<p class="empty">正在加载科室…</p>';

  try {
    if (!state.departments.length) await loadBase();
    // 号源变动不频繁，同一次会话里拉一次就够
    if (!state.schedules.length) {
      state.schedules = await api('/api/schedules?only_available=true');
    }
    renderDeptGrid();
  } catch (err) {
    grid.innerHTML = `<p class="error">${esc(err.message)}</p>`;
  }
}

function renderDeptGrid() {
  const grid = $('#dept-grid');
  const counts = new Map();
  state.schedules.forEach((s) => {
    const doc = state.doctorById.get(s.doctor_id);
    if (!doc) return;
    counts.set(doc.department_id, (counts.get(doc.department_id) || 0) + s.remaining);
  });

  grid.innerHTML = state.departments.map((d) => {
    const n = counts.get(d.id) || 0;
    return `
      <button class="dept-btn" data-dept="${d.id}">
        <span class="n">${esc(d.name)}</span>
        <span class="c${n ? '' : ' none'}">${n ? `可约 ${n} 个号` : '暂无可约号'}</span>
      </button>`;
  }).join('');

  grid.querySelectorAll('[data-dept]').forEach((btn) => {
    btn.addEventListener('click', () => openDepartment(Number(btn.dataset.dept)));
  });
}

/** 进入某个科室看号。分诊推荐卡片也直接调这个函数。 */
function openDepartment(deptId) {
  $('#book-step1').hidden = true;
  $('#book-step2').hidden = false;
  const dept = state.deptById.get(deptId);
  $('#book-dept-name').textContent = dept ? dept.name : '号源';
  renderDeptSchedules(deptId);
}

function backToDepartments() {
  $('#book-step2').hidden = true;
  $('#book-step1').hidden = false;
}

$('#btn-back-dept').addEventListener('click', backToDepartments);

function renderDeptSchedules(deptId) {
  const list = $('#schedule-list');
  const rows = state.schedules
    .map((s) => ({ ...s, doctor: state.doctorById.get(s.doctor_id) }))
    .filter((s) => s.doctor && s.doctor.department_id === deptId)
    .sort((a, b) => (a.work_date + a.period).localeCompare(b.work_date + b.period))
    .slice(0, 12);

  if (!rows.length) {
    list.innerHTML = '<p class="empty">这个科最近几天还没有放号，<br>换一个科室看看。</p>';
    return;
  }

  // 号源按日期分组显示，免得一长串看着像重复
  const byDate = new Map();
  rows.forEach((s) => {
    if (!byDate.has(s.work_date)) byDate.set(s.work_date, []);
    byDate.get(s.work_date).push(s);
  });

  list.innerHTML = [...byDate.entries()].map(([d, items]) => `
    <p class="date-head">${prettyDate(d)}</p>
    ${items.map((s) => `
      <button class="slot" data-slot="${s.id}">
        <span class="who">
          <span class="doc">${esc(s.doctor.name)}<span class="title">${esc(s.doctor.title)}</span></span>
          <span class="meta">${esc(s.period)} ${(s.start_time || '').slice(0, 5)}–${(s.end_time || '').slice(0, 5)}</span>
        </span>
        <span class="left">余号 ${s.remaining}</span>
      </button>`).join('')}
  `).join('');

  list.querySelectorAll('[data-slot]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const slot = rows.find((s) => String(s.id) === btn.dataset.slot);
      openSheet(slot);
    });
  });
}

/* ---------------- 确认与提交 ---------------- */

function openSheet(slot, deptId) {
  state.pendingSlot = slot;
  state.pendingDeptId = deptId;
  const dept = state.deptById.get(deptId);
  $('#sheet-info').innerHTML = `
    <div><span class="k">医生：</span><b>${esc(slot.doctor.name)} ${esc(slot.doctor.title)}</b></div>
    <div><span class="k">科室：</span><b>${esc(dept ? dept.name : '')}</b></div>
    <div><span class="k">时间：</span><b>${prettyDate(slot.work_date)} ${periodRange(slot)}</b></div>
    <div><span class="k">余号：</span><b>${slot.remaining} 个</b></div>`;
  $('#sheet-error').hidden = true;
  $('#sheet').hidden = false;
}

document.querySelectorAll('[data-close]').forEach((el) => {
  el.addEventListener('click', () => { $('#sheet').hidden = true; });
});
document.querySelectorAll('[data-pickup-close]').forEach((el) => {
  el.addEventListener('click', () => { $('#pickup-sheet').hidden = true; });
});

$('#btn-confirm').addEventListener('click', submitBooking);

async function submitBooking() {
  const slot = state.pendingSlot;
  const name = $('#p-name').value.trim();
  const phone = $('#p-phone').value.trim();
  const errBox = $('#sheet-error');

  if (name.length < 2) return showSheetError('请填写就诊人的真实姓名。');
  if (!/^1[3-9]\d{9}$/.test(phone)) return showSheetError('手机号填得不对，应该是 11 位数字。');

  const btn = $('#btn-confirm');
  btn.disabled = true;
  btn.textContent = '正在提交…';

  try {
    const appt = await api('/api/appointments', {
      method: 'POST',
      body: JSON.stringify({
        name,
        phone,
        schedule_id: slot.id,
        triage_department_id: state.triage && state.triage.candidates.length
          ? state.triage.candidates[0].department_id : null,
      }),
    });
    localStorage.setItem('lastPhone', phone);     // 方便下次查号
    $('#sheet').hidden = true;
    showDone(appt);
    state.schedules = [];                          // 号源变了，下次重新拉
    backToDepartments();                           // 回到科室列表，避免停留在旧号源上
  } catch (err) {
    showSheetError(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '确认预约';
  }
}

function showSheetError(msg) {
  const box = $('#sheet-error');
  box.textContent = msg;
  box.hidden = false;
}

function showDone(appt) {
  $('#done-info').innerHTML = `
    <div><span class="k">预约号：</span><b>${esc(appt.id)} 号</b></div>
    <div><span class="k">医生：</span><b>${esc(appt.doctor_name)} ${esc(appt.doctor_title)}</b></div>
    <div><span class="k">科室：</span><b>${esc(appt.department_name)}</b></div>
    <div><span class="k">时间：</span><b>${prettyDate(appt.work_date)} ${esc(appt.period)}</b></div>`;
  $('#done').hidden = false;
}

$('#btn-done-close').addEventListener('click', () => {
  $('#done').hidden = true;
  $('#p-name').value = '';
  $('#p-phone').value = localStorage.getItem('lastPhone') || '';
  switchView('mine');
  loadMine();
});

/* ---------------- 四、取药 ---------------- */

$('#btn-pickup').addEventListener('click', loadPrescriptions);
$('#med-search').addEventListener('input', debounce(loadMedicines, 250));

function debounce(fn, wait) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

async function loadPrescriptions() {
  const phone = $('#pickup-phone').value.trim();
  const box = $('#rx-list');
  box.hidden = false;

  if (!/^1[3-9]\d{9}$/.test(phone)) {
    box.innerHTML = '<p class="error">请填 11 位手机号。</p>';
    return;
  }
  box.innerHTML = '<p class="empty">正在查询…</p>';

  try {
    const list = await api('/api/prescriptions?phone=' + encodeURIComponent(phone));
    if (!list.length) {
      box.innerHTML = '<p class="empty">这个手机号还没有处方。<br>看完病医生开方后就能在这里看到。</p>';
      return;
    }
    box.innerHTML = list.map(renderPrescription).join('');
    bindPrescriptionActions(box);
  } catch (err) {
    box.innerHTML = `<p class="error">${esc(err.message)}</p>`;
  }
}

function renderPrescription(rx) {
  const items = rx.items.map((it) => `
    <li>
      <b>${esc(it.name)}</b> ${esc(it.spec)}
      <span class="qty">×${it.quantity}${esc(it.unit)}</span>
      ${it.dosage ? `<span class="dosage">${esc(it.dosage)}</span>` : ''}
    </li>`).join('');

  // 状态翻译成人话，并且只给"下一步该做什么"
  let action = '';
  if (rx.status === 'pending_pay') {
    action = `<button class="primary go" data-pay="${rx.id}">去缴费 ${rx.total_yuan} 元</button>`;
  } else if (rx.status === 'pending_pickup') {
    action = rx.pickup_date
      ? `<div class="pickup-set">取药时间：<b>${esc(rx.pickup_date)} ${esc(rx.pickup_period)}</b>
           <button class="link" data-pickup="${rx.id}">改时间</button></div>`
      : `<button class="primary go" data-pickup="${rx.id}">选取药时间</button>`;
  } else if (rx.status === 'picked') {
    action = '<div class="pickup-set done">已取药</div>';
  }

  return `
    <div class="pick${rx.status === 'cancelled' ? ' alt' : ''}">
      <div class="rx-head">
        <span class="rx-code">处方号 ${esc(rx.code)}</span>
        <span class="rx-status s-${esc(rx.status)}">${esc(rx.status_text)}</span>
      </div>
      <ul class="rx-items">${items}</ul>
      <div class="rx-total">合计 <b>${rx.total_yuan}</b> 元</div>
      ${action}
    </div>`;
}

function bindPrescriptionActions(box) {
  box.querySelectorAll('[data-pay]').forEach((btn) => {
    btn.addEventListener('click', () => payPrescription(btn.dataset.pay));
  });
  box.querySelectorAll('[data-pickup]').forEach((btn) => {
    btn.addEventListener('click', () => openPickupSheet(btn.dataset.pickup));
  });
}

async function payPrescription(id) {
  if (!confirm('确认缴费吗？缴费后药房会开始备药。')) return;
  try {
    await api(`/api/prescriptions/${id}/pay`, { method: 'POST' });
    loadPrescriptions();
  } catch (err) {
    alert(err.message);
  }
}

async function openPickupSheet(id) {
  let options = [];
  try {
    options = await api('/api/pickup-options');
  } catch (err) {
    alert(err.message);
    return;
  }

  $('#pickup-info').innerHTML = '选一个方便的取药时间，药房会按这个时段备药。';
  $('#pickup-slots').innerHTML = options.map((opt) => `
    <div class="pickup-day">
      <div class="pickup-day-label">${esc(opt.label)}</div>
      <div class="pickup-periods">
        ${opt.periods.map((p) => `
          <button class="period-btn" data-date="${opt.date}" data-period="${esc(p)}">${esc(p)}</button>
        `).join('')}
      </div>
    </div>`).join('');
  $('#pickup-error').hidden = true;
  $('#pickup-sheet').hidden = false;

  $('#pickup-slots').querySelectorAll('[data-date]').forEach((btn) => {
    btn.addEventListener('click', () => choosePickup(id, btn.dataset.date, btn.dataset.period));
  });
}

async function choosePickup(id, pickupDate, period) {
  try {
    await api(`/api/prescriptions/${id}/pickup`, {
      method: 'POST',
      body: JSON.stringify({ pickup_date: pickupDate, period }),
    });
    $('#pickup-sheet').hidden = true;
    loadPrescriptions();
  } catch (err) {
    const box = $('#pickup-error');
    box.textContent = err.message;
    box.hidden = false;
  }
}

$('#btn-pickup-close').addEventListener('click', () => { $('#pickup-sheet').hidden = true; });

async function loadMedicines() {
  const box = $('#med-list');
  const keyword = $('#med-search').value.trim();
  if (!box) return;

  box.innerHTML = '<p class="empty">正在加载…</p>';
  try {
    const query = keyword ? '?keyword=' + encodeURIComponent(keyword) : '';
    const list = await api('/api/medicines' + query);
    if (!list.length) {
      box.innerHTML = '<p class="empty">没找到这个药。</p>';
      return;
    }
    box.innerHTML = list.map((m) => `
      <div class="med-row">
        <div class="med-name">${esc(m.name)}</div>
        <div class="med-meta">${esc(m.spec)} · ${esc(m.manufacturer || '')}</div>
        <div class="med-price">
          ${m.price_yuan} 元
          ${m.stock > 0
            ? `<span class="stock">有货</span>`
            : `<span class="stock none">暂时缺货</span>`}
        </div>
      </div>`).join('');
  } catch (err) {
    box.innerHTML = `<p class="error">${esc(err.message)}</p>`;
  }
}

/* ---------------- 三、我的预约 ---------------- */

$('#btn-mine').addEventListener('click', loadMine);

async function loadMine() {
  const phone = $('#my-phone').value.trim();
  const box = $('#mine-list');
  box.hidden = false;

  if (!/^1[3-9]\d{9}$/.test(phone)) {
    box.innerHTML = '<p class="error">请填 11 位手机号。</p>';
    return;
  }
  box.innerHTML = '<p class="empty">正在查询…</p>';

  try {
    const list = await api('/api/appointments?phone=' + encodeURIComponent(phone));
    if (!list.length) {
      box.innerHTML = '<p class="empty">这个手机号还没有预约记录。</p>';
      return;
    }
    box.innerHTML = list.map(renderAppointment).join('');
    box.querySelectorAll('[data-cancel]').forEach((btn) => {
      btn.addEventListener('click', () => cancelAppointment(btn.dataset.cancel));
    });
  } catch (err) {
    box.innerHTML = `<p class="error">${esc(err.message)}</p>`;
  }
}

function renderAppointment(a) {
  const active = a.status === 'booked';
  return `
    <div class="pick${active ? '' : ' alt'}">
      <div class="name">${esc(a.department_name)} · ${esc(a.doctor_name)}</div>
      <div class="why">
        ${prettyDate(a.work_date)} ${esc(a.period)}<br>
        预约号 ${esc(a.id)} 号
        ${active ? '' : ' · <b>已取消</b>'}
      </div>
      ${active ? `<button class="ghost go" data-cancel="${a.id}">取消这条预约</button>` : ''}
    </div>`;
}

async function cancelAppointment(id) {
  if (!confirm('确定要取消这条预约吗？取消后号会放回号池。')) return;
  try {
    await api(`/api/appointments/${id}/cancel`, { method: 'POST' });
    loadMine();
  } catch (err) {
    alert(err.message);
  }
}

/* ---------------- 启动 ---------------- */

(async function init() {
  const saved = localStorage.getItem('lastPhone') || '';
  ['#p-phone', '#my-phone', '#pickup-phone'].forEach((sel) => {
    const el = $(sel);
    if (el) el.value = saved;
  });
  try {
    await loadBase();
  } catch (err) {
    console.error('基础数据加载失败', err);
  }
})();
