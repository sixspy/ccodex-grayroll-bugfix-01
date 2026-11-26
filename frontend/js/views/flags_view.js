import { api } from "../api.js";
const { ref, computed, onMounted } = Vue;
export const FlagsView = {
  setup() {
    const items = ref([]);
    const form = ref({ key: "promo.banner_v3", title: "活动横幅 v3", description: "首页横幅实验", owner: "writer" });
    const err = ref("");
    const ok = ref("");
    async function reload() {
      err.value = "";
      items.value = (await api("/api/flags")).items || [];
    }
    // 五页同源：与后端 flags.key 唯一判据一致，前端列表内聚合同键多行作为撞键告警
    const collisions = computed(() => {
      const m = new Map();
      for (const it of items.value) m.set(it.key, (m.get(it.key) || 0) + 1);
      return [...m.entries()].filter(([, c]) => c > 1).map(([key, c]) => ({ key, c }));
    });
    async function create() {
      err.value = ""; ok.value = "";
      try {
        const res = await api("/api/flags", { method: "POST", body: JSON.stringify(form.value) });
        ok.value = `已登记开关键 ${res.key}（id=${res.id}，状态：${res.status}）`;
        await reload();
      } catch (e) {
        // 撞键拒写(400)与创建锁争用(409)均在此显式呈现，不允许假成功
        err.value = e.message || String(e);
      }
    }
    onMounted(reload);
    return { items, form, err, ok, collisions, create, reload };
  },
  template: `
  <div class="grid">
    <div class="panel span-5">
      <h2>登记开关</h2>
      <p class="hint">同 key 仅允许一行正式开关：撞键第二笔正式零增拒写；并发创建走开关键创建锁，败方仅留争用痕。</p>
      <div class="field"><label>开关键</label><input v-model="form.key" /></div>
      <div class="field"><label>标题</label><input v-model="form.title" /></div>
      <div class="field"><label>说明</label><textarea v-model="form.description" rows="2"></textarea></div>
      <button class="btn" @click="create">创建</button>
      <div class="err" v-if="err" style="margin-top:0.6rem">拒写：{{ err }}</div>
      <div class="muted" v-if="ok" style="margin-top:0.6rem">{{ ok }}</div>
    </div>
    <div class="panel span-7">
      <h2>开关列表</h2>
      <div class="err" v-if="collisions.length" style="margin-bottom:0.6rem">
        撞键告警：<span v-for="c in collisions" :key="c.key"><code>{{ c.key }}</code> × {{ c.c }} 行；</span>
        求值与发布将拒决，请先核销撞键。
      </div>
      <table>
        <thead><tr><th>键</th><th>标题</th><th>状态</th><th>负责人</th></tr></thead>
        <tbody>
          <tr v-for="it in items" :key="it.id">
            <td><code>{{ it.key }}</code></td><td>{{ it.title }}</td>
            <td><span class="pill" :class="{'danger': it.status==='killed'}">{{ it.status_label || it.status }}</span></td>
            <td>{{ it.owner }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>`
};
