import { api } from "../api.js";
const { ref, computed, onMounted } = Vue;
export const EnvsView = {
  setup() {
    const items = ref([]);
    const audit = ref(null);
    const err = ref("");
    const msg = ref("");
    const busy = ref(false);
    const form = ref({ code: "", title: "", risk_level: 1 });

    const duplicates = computed(() => audit.value?.duplicate_codes || []);
    const consistent = computed(() => audit.value?.consistent !== false);

    async function reload() {
      err.value = "";
      try {
        const [envRes, auditRes] = await Promise.all([
          api("/api/envs"),
          api("/api/rules/completeness").catch(() => null),
        ]);
        items.value = envRes.items || [];
        audit.value = auditRes;
      } catch (e) {
        err.value = e.message || String(e);
      }
    }

    async function createEnv() {
      err.value = "";
      msg.value = "";
      if (!form.value.code || !form.value.title) {
        err.value = "环境编码与名称必填";
        return;
      }
      busy.value = true;
      try {
        const row = await api("/api/envs", {
          method: "POST",
          body: JSON.stringify({
            code: form.value.code.trim(),
            title: form.value.title.trim(),
            risk_level: Number(form.value.risk_level) || 1,
          }),
        });
        msg.value = `已登记环境 ${row.code}（#${row.id}），同编码第二笔将被整笔拒写`;
        form.value = { code: "", title: "", risk_level: 1 };
        await reload();
      } catch (e) {
        // 甲路径：400 同编码已存在 / 409 互斥败方，直接把封死原因摆到台面。
        err.value = e.message || String(e);
      } finally {
        busy.value = false;
      }
    }

    async function reconcile() {
      err.value = "";
      msg.value = "";
      busy.value = true;
      try {
        const report = await api("/api/envs/reconcile", {
          method: "POST",
          body: JSON.stringify({}),
        });
        msg.value = report.reconciled_codes
          ? `已同事务并轨 ${report.reconciled_codes} 组编码，规则改挂单行，清理环境行 ${report.removed_env_ids.length} 条`
          : "账实已相符，无需并轨";
        await reload();
      } catch (e) {
        err.value = e.message || String(e);
      } finally {
        busy.value = false;
      }
    }

    onMounted(reload);
    return {
      items, audit, err, msg, busy, form,
      duplicates, consistent, reload, createEnv, reconcile,
    };
  },
  template: `
  <div class="grid">
    <div class="panel span-5">
      <h2>登记环境编码（甲路径）</h2>
      <p class="hint">同一编码只允许一行正式环境；已有同编码再提交，第二笔整笔拒写、正式册零增。创建受「环境编码创建互斥」保护。</p>
      <div class="field"><label>环境编码 code</label><input v-model="form.code" placeholder="例如 staging-east" /></div>
      <div class="field"><label>环境名称</label><input v-model="form.title" placeholder="例如 东部预发" /></div>
      <div class="field"><label>风险等级 1-5</label><input type="number" min="0" max="10" v-model.number="form.risk_level" /></div>
      <button class="btn" :disabled="busy" @click="createEnv">创建环境</button>
      <p class="muted" v-if="msg" style="color:#1a7f37">{{ msg }}</p>
      <div class="err" v-if="err">{{ err }}</div>
    </div>

    <div class="panel span-7">
      <h2>环境编码册</h2>
      <div class="err" v-if="err && !items.length">{{ err }}</div>

      <div v-if="duplicates.length" class="err" style="margin-bottom:.7rem">
        <strong>账实不符：检出 {{ duplicates.length }} 组同码双行环境，规则外键可能分裂挂接，齐套状态不予报绿。</strong>
        <ul style="margin:.4rem 0 0 1.1rem">
          <li v-for="d in duplicates" :key="d.code">
            <code>{{ d.code }}</code> 行 IDs={{ d.env_ids.join(', ') }}；保留 canonical #{{ d.canonical_id }}
          </li>
        </ul>
        <button class="btn secondary" style="margin-top:.6rem" :disabled="busy" @click="reconcile">
          同事务联查并轨：规则改挂单行
        </button>
      </div>
      <p class="hint" v-else-if="audit">环境编码唯一 × 规则外键联查：账实相符（{{ audit.env_code_count }} 个编码 / {{ audit.env_total }} 行）。</p>

      <table>
        <thead><tr><th>ID</th><th>代码</th><th>名称</th><th>风险等级</th></tr></thead>
        <tbody>
          <tr v-for="it in items" :key="it.id"
              :style="duplicates.some(d => d.env_ids.includes(it.id)) ? 'background:#fdecea' : ''">
            <td>{{ it.id }}</td>
            <td><code>{{ it.code }}</code></td>
            <td>{{ it.title }}</td>
            <td>{{ it.risk_level }}</td>
          </tr>
        </tbody>
      </table>
      <button class="btn secondary" style="margin-top:0.7rem" @click="reload">刷新</button>
    </div>
  </div>`
};
