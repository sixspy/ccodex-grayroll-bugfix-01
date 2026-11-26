import { api } from "../api.js";
const { ref, computed, onMounted } = Vue;
export const RulesView = {
  setup() {
    const official = ref([]); const shadows = ref([]); const audit = ref(null);
    const form = ref({ flag_id: 3, env_id: 2, kind: "percent", percent: 10, cohort_id: null });
    const err = ref(""); const msg = ref(""); const busy = ref(false);

    const duplicates = computed(() => audit.value?.duplicate_codes || []);
    const ambiguousCells = computed(() => (audit.value?.coverage || []).filter(c => c.ambiguous));

    async function reload() {
      const res = await api("/api/rules");
      official.value = res.official || []; shadows.value = res.shadows || [];
      audit.value = res.audit || null;
    }
    async function shadowBind() {
      err.value = ""; msg.value = "";
      try {
        const body = { ...form.value, cohort_id: form.value.cohort_id || null };
        await api("/api/rules/shadow-bind", { method: "POST", body: JSON.stringify(body) });
        msg.value = "影子绑定已写入"; await reload();
      } catch (e) { err.value = e.message || String(e); }
    }
    async function promote(id) {
      err.value = "";
      try { await api(`/api/rules/shadow/${id}/promote`, { method: "POST", body: "{}" }); await reload(); }
      catch (e) { err.value = e.message || String(e); }
    }
    async function reconcileEnvCodes() {
      err.value = ""; msg.value = ""; busy.value = true;
      try {
        const rep = await api("/api/envs/reconcile", { method: "POST", body: JSON.stringify({}) });
        msg.value = rep.reconciled_codes
          ? `已并轨 ${rep.reconciled_codes} 组双行编码，规则只挂单行`
          : "无需并轨";
        await reload();
      } catch (e) { err.value = e.message || String(e); }
      finally { busy.value = false; }
    }
    onMounted(reload);
    return {
      official, shadows, audit, form, err, msg, busy,
      duplicates, ambiguousCells,
      shadowBind, promote, reconcileEnvCodes, reload,
    };
  },
  template: `
  <div class="grid">
    <div class="panel span-12" v-if="audit">
      <h2>环境编码 × 规则外键账实核对</h2>
      <p class="hint">齐套绿灯只有在『无同码双行、无孤儿/歧义挂接』且逐格有唯一正式规则时才成立；双行环境按单编码折叠出的齐套按假绿处理。</p>
      <p>
        账实一致：
        <strong :style="audit.consistent ? 'color:#1a7f37' : 'color:#c0392b'">
          {{ audit.consistent ? '是' : '否' }}
        </strong>
        ｜齐套：<strong>{{ audit.complete ? '是' : '否' }}</strong>
        ｜总绿灯：
        <strong :style="audit.green ? 'color:#1a7f37' : 'color:#c0392b'">
          {{ audit.green ? '绿' : '不亮（防假绿）' }}
        </strong>
      </p>
      <div class="err" v-if="duplicates.length" style="margin:.5rem 0">
        同码双行：
        <span v-for="d in duplicates" :key="d.code" style="margin-right:1rem">
          <code>{{ d.code }}</code> [{{ d.env_ids.join(',') }}] → canonical #{{ d.canonical_id }}
        </span>
        <button class="btn secondary" style="margin-left:.6rem" :disabled="busy" @click="reconcileEnvCodes">同事务并轨 / 规则改挂单行</button>
      </div>
      <div class="err" v-if="ambiguousCells.length && !duplicates.length" style="margin:.5rem 0">
        {{ ambiguousCells.length }} 个规则格存在多条/歧义挂接。
      </div>
      <p class="muted" v-if="msg" style="color:#1a7f37">{{ msg }}</p>
      <div class="err" v-if="err">{{ err }}</div>
    </div>

    <div class="panel span-5">
      <h2>影子绑定（乙）</h2>
      <p class="hint">只写影子草案，正式规则升正前零改。环境编码唯一性仍由甲路径创建口保证，甲乙不混用。</p>
      <div class="field"><label>开关ID</label><input type="number" v-model.number="form.flag_id" /></div>
      <div class="field"><label>环境ID</label><input type="number" v-model.number="form.env_id" /></div>
      <div class="field"><label>类型</label>
        <select v-model="form.kind"><option>percent</option><option>cohort</option><option>percent_and_cohort</option></select>
      </div>
      <div class="field"><label>比例</label><input type="number" v-model.number="form.percent" /></div>
      <button class="btn" @click="shadowBind">写入影子</button>
      <p class="muted" v-if="msg">{{ msg }}</p>
      <div class="err" v-if="err">{{ err }}</div>
    </div>
    <div class="panel span-7">
      <h2>正式规则</h2>
      <table><thead><tr><th>开关</th><th>环境</th><th>类型</th><th>%</th><th>版本</th></tr></thead>
      <tbody>
        <tr v-for="r in official" :key="r.id">
          <td>{{ r.flag_key }}</td><td>{{ r.env_code }}</td><td>{{ r.kind }}</td><td>{{ r.percent }}</td><td>v{{ r.version }}</td>
        </tr>
      </tbody></table>
      <h2 style="margin-top:1rem">影子草案</h2>
      <table><thead><tr><th>ID</th><th>开关</th><th>环境</th><th>状态</th><th></th></tr></thead>
      <tbody>
        <tr v-for="s in shadows" :key="s.id">
          <td>{{ s.id }}</td><td>{{ s.flag_id }}</td><td>{{ s.env_id }}</td><td>{{ s.status }}</td>
          <td><button class="btn secondary" v-if="s.status==='open'" @click="promote(s.id)">升正</button></td>
        </tr>
      </tbody></table>
    </div>
  </div>`
};
